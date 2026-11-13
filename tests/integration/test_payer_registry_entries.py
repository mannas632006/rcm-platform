"""The payer registry in the scrubber (M4.10, BR-REG-01..03, UR-18).

M4.10 acceptance: "Stale entry cannot drive a BLOCKING decision."
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ProgrammingError

from rcm.decision import registry
from rcm.decision.rules.evaluate import Verdict
from rcm.decision.rules.scrub import scrub_claim
from rcm.decision.rules.store import publish_ruleset
from rcm.domain.claim import Claim
from rcm.platform.audit import DecisionRecord, DecisionType
from rcm.platform.config import get_settings
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ResourceNotFoundError, ValidationFailedError
from tests.integration.test_acknowledgement_loading import CLIENT_A, submit
from tests.integration.test_rules_engine import TABLES

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 3)
OWNER = "registry.owner"


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE payer_registry_entry, {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def claim(tmp_path: Path) -> tuple[uuid.UUID, str]:
    """A submitted claim for CPT 99203 with no authorisation, and a published
    ruleset holding no rules: whatever blocks it is the registry."""
    root = tmp_path / "raw"
    root.mkdir()
    submit("CLM-REG", root, control="0001")
    with session_scope() as session:
        publish_ruleset(session, published_by=OWNER)
        claim_id, payer_id = session.execute(select(Claim.claim_id, Claim.payer_id)).one()
    return claim_id, payer_id


def record(payer_id: str, *, verified: date, auth: bool | None = True) -> int:
    with session_scope() as session:
        return registry.record_entry(
            session,
            payer_id=payer_id,
            cpt="99203",
            prior_auth_required=auth,
            source_url="https://payer.example/policy/99203",
            verified_on=verified,
            verified_by=OWNER,
            recorded_by=OWNER,
            reason="payer policy bulletin",
            today=TODAY,
        ).version


def scrub(claim_id: uuid.UUID) -> Verdict:
    with session_scope() as session:
        result = scrub_claim(session, claim_id=claim_id, client_id=CLIENT_A, today=TODAY)
        return result.evaluation.verdict


@pytest.mark.invariant
def test_stale_payer_rule_cannot_block(claim: tuple[uuid.UUID, str]) -> None:
    """BR-REG-02 / UR-18: the same entry only advises once stale, and blocks
    again when re-verified."""
    claim_id, payer_id = claim
    limit = get_settings().payer_registry_staleness_days
    record(payer_id, verified=TODAY - timedelta(days=limit + 1))
    assert scrub(claim_id) is Verdict.ADVISE

    with session_scope() as session:
        registry.verify_entry(
            session,
            payer_id=payer_id,
            cpt="99203",
            verified_on=TODAY,
            verified_by=OWNER,
            today=TODAY,
        )
    assert scrub(claim_id) is Verdict.BLOCK


def test_the_scrub_records_the_registry_version_it_used(claim: tuple[uuid.UUID, str]) -> None:
    """BR-REG-03: claims record which registry version applied."""
    claim_id, payer_id = claim
    record(payer_id, verified=TODAY, auth=False)
    version = record(payer_id, verified=TODAY)
    scrub(claim_id)
    with session_scope() as session:
        decision = session.scalars(
            select(DecisionRecord).where(
                DecisionRecord.decision_type == str(DecisionType.SCRUB),
                DecisionRecord.entity_id == claim_id,
            )
        ).one()
    (used,) = decision.input_snapshot["registry"]
    assert (used["cpt"], used["version"], used["stale"]) == ("99203", version, False)
    assert version == 2


def test_a_requirement_changed_overnight_is_what_the_next_scrub_reads(
    claim: tuple[uuid.UUID, str],
) -> None:
    """EC-40: the payer starts requiring authorisation. Recording that is a
    new registry version, and the claim that passed yesterday is blocked by
    the next scrub with no rule republished. Each scrub names the version it
    read, so yesterday's pass still explains itself."""
    claim_id, payer_id = claim
    before = record(payer_id, verified=TODAY, auth=False)
    assert scrub(claim_id) is Verdict.PASS

    after = record(payer_id, verified=TODAY, auth=True)
    assert scrub(claim_id) is Verdict.BLOCK

    with session_scope() as session:
        decisions = session.scalars(
            select(DecisionRecord)
            .where(
                DecisionRecord.decision_type == str(DecisionType.SCRUB),
                DecisionRecord.entity_id == claim_id,
            )
            .order_by(DecisionRecord.decided_at, DecisionRecord.decision_id)
        ).all()
    read = [[used["version"] for used in d.input_snapshot["registry"]] for d in decisions]
    assert read == [[before], [after]]
    assert [d.output["verdict"] for d in decisions] == ["PASS", "BLOCK"]


def test_a_change_is_a_new_version_and_the_old_one_stays(
    claim: tuple[uuid.UUID, str], app_engine: Engine
) -> None:
    """BR-REG-03: versioned. Insert-only by grant, so history cannot be edited."""
    _, payer_id = claim
    record(payer_id, verified=TODAY, auth=False)
    record(payer_id, verified=TODAY, auth=True)
    with session_scope() as session:
        history = registry.history(session, payer_id=payer_id, cpt="99203")
    assert [(e.version, e.prior_auth_required) for e in history] == [(1, False), (2, True)]
    for statement in (
        "UPDATE payer_registry_entry SET prior_auth_required = false",
        "DELETE FROM payer_registry_entry",
    ):
        with app_engine.connect() as conn, pytest.raises(ProgrammingError) as refused:
            conn.execute(text(statement))
        assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)


@pytest.mark.parametrize(
    ("verified", "verifier"),
    [(TODAY + timedelta(days=1), OWNER), (TODAY, "  ")],
)
def test_an_entry_names_a_real_verification(
    claim: tuple[uuid.UUID, str], verified: date, verifier: str
) -> None:
    """BR-REG-01: a verifier, on a date that has happened."""
    _, payer_id = claim
    with pytest.raises(ValidationFailedError), session_scope() as session:
        registry.record_entry(
            session,
            payer_id=payer_id,
            cpt="99203",
            prior_auth_required=True,
            source_url=None,
            verified_on=verified,
            verified_by=verifier,
            recorded_by=OWNER,
            reason="payer policy bulletin",
            today=TODAY,
        )


def test_an_entry_for_an_unknown_payer_is_refused(claim: tuple[uuid.UUID, str]) -> None:
    with pytest.raises(ResourceNotFoundError), session_scope() as session:
        registry.record_entry(
            session,
            payer_id="NO-SUCH-PAYER",
            cpt="99203",
            prior_auth_required=True,
            source_url=None,
            verified_on=TODAY,
            verified_by=OWNER,
            recorded_by=OWNER,
            reason="payer policy bulletin",
            today=TODAY,
        )


def test_verifying_an_entry_that_does_not_exist_is_refused(claim: tuple[uuid.UUID, str]) -> None:
    _, payer_id = claim
    with pytest.raises(ResourceNotFoundError), session_scope() as session:
        registry.verify_entry(
            session,
            payer_id=payer_id,
            cpt="99203",
            verified_on=TODAY,
            verified_by=OWNER,
            today=TODAY,
        )
