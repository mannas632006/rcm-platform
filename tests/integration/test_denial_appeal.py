"""Appealing a denial (M5.7 slice 2, UR-09, BR-M11-04, ADR-0017).

UR-09: an appeal letter is always reviewed by a person before it goes; no
send path bypasses review. The draft is a template with bracketed prompts a
person must replace, signing binds to the exact text read, and a stale or
unreviewed version cannot be signed.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rcm.domain.claim import Claim
from rcm.domain.claim_state import ClaimState
from rcm.platform.auth import Principal, Role
from rcm.platform.config import get_settings
from rcm.platform.db import session_scope
from rcm.platform.errors import (
    AuthorizationDeniedError,
    StateTransitionError,
    ValidationFailedError,
)
from rcm.work import appeals, dispositions
from rcm.work.models import AppealSignoff, WorkItem
from tests.integration.test_acknowledgement_loading import CLIENT_A
from tests.integration.test_api_shell import api, bearer, clean  # noqa: F401
from tests.integration.test_denial_classification import NOW
from tests.integration.test_denial_write_off import SPECIALIST, denied, person

pytestmark = pytest.mark.integration

REVIEWED = (
    "To whom it may concern,\n\nWe appeal the denial of claim CLM-WO. The record shows the "
    "visit was necessary: see the attached notes.\n\nSincerely,\nDana"
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def draft(item: uuid.UUID) -> appeals.AppealView:
    with session_scope() as session:
        return appeals.draft_appeal(session, principal=SPECIALIST, work_item_id=item, now=NOW)


def revise(appeal_id: uuid.UUID, letter: str) -> appeals.AppealView:
    with session_scope() as session:
        return appeals.revise_appeal(
            session, principal=SPECIALIST, appeal_id=appeal_id, letter=letter, now=NOW
        )


def sign(appeal_id: uuid.UUID, version: int, who: Principal = SPECIALIST) -> appeals.AppealView:
    with session_scope() as session:
        return appeals.sign_and_send(
            session, principal=who, appeal_id=appeal_id, version=version, now=NOW
        )


def claim_state() -> str:
    with session_scope() as session:
        state = session.scalar(select(Claim.state).where(Claim.submitter_claim_id == "CLM-WO"))
    assert state is not None
    return state


def test_a_draft_names_the_claim_and_its_codes_and_asks_for_review(root: Path) -> None:
    view = draft(denied(root))
    assert view.status == "DRAFT"
    assert view.version == 1
    assert "CLM-WO" in view.letter
    assert "CO-50" in view.letter  # PD-3: group and reason together
    assert "[" in view.letter  # prompts a person must replace
    assert view.evidence  # a checklist for the category
    assert claim_state() == str(ClaimState.DENIED)


def test_drafting_twice_opens_the_same_appeal(root: Path) -> None:
    item = denied(root)
    assert draft(item).appeal_id == draft(item).appeal_id


@pytest.mark.invariant
def test_appeal_requires_human_signoff(root: Path) -> None:
    """UR-09: an unreviewed draft -- prompts still in it -- cannot be sent."""
    view = draft(denied(root))
    with pytest.raises(ValidationFailedError):
        sign(view.appeal_id, view.version)
    assert claim_state() == str(ClaimState.DENIED)
    with session_scope() as session:
        assert session.scalars(select(AppealSignoff)).all() == []


@pytest.mark.invariant
def test_signing_binds_to_the_exact_text_read(root: Path) -> None:
    item = denied(root)
    first = draft(item)
    reviewed = revise(first.appeal_id, REVIEWED)
    assert reviewed.version == 2
    sent = sign(reviewed.appeal_id, reviewed.version)
    assert sent.status == "SENT"
    with session_scope() as session:
        (signoff,) = session.scalars(select(AppealSignoff)).all()
        assert signoff.version == 2
        assert signoff.letter_sha256 == hashlib.sha256(REVIEWED.encode()).hexdigest()
        assert signoff.signed_by == SPECIALIST.subject
        found = session.get(WorkItem, item)
        assert found is not None
        assert (found.state, found.outcome) == ("Resolved", "APPEALED")
        claim = session.scalar(select(Claim).where(Claim.submitter_claim_id == "CLM-WO"))
        assert claim is not None
        assert claim.state == str(ClaimState.APPEALING)
        days = get_settings().appeal_follow_up_days
        assert claim.next_action_due == NOW.date() + timedelta(days=days)


def test_an_older_version_cannot_be_signed(root: Path) -> None:
    """The signer signs what is current, not a version someone has since changed."""
    view = draft(denied(root))
    revise(view.appeal_id, REVIEWED)
    revise(view.appeal_id, REVIEWED + "\n\nP.S. Records enclosed.")
    with pytest.raises(StateTransitionError):
        sign(view.appeal_id, 2)


def test_signing_needs_appeal_sign(root: Path) -> None:
    view = draft(denied(root))
    reviewed = revise(view.appeal_id, REVIEWED)
    with pytest.raises(AuthorizationDeniedError):
        sign(reviewed.appeal_id, reviewed.version, who=person("ar.amy", Role.AR_SPECIALIST))


def test_a_sent_appeal_is_not_sent_again(root: Path) -> None:
    view = draft(denied(root))
    reviewed = revise(view.appeal_id, REVIEWED)
    sign(reviewed.appeal_id, reviewed.version)
    with pytest.raises(StateTransitionError):
        sign(reviewed.appeal_id, reviewed.version)


def test_no_appeal_while_a_write_off_waits(root: Path) -> None:
    item = denied(root)
    with session_scope() as session:
        dispositions.request_write_off(
            session,
            principal=SPECIALIST,
            work_item_id=item,
            reason=dispositions.WriteOffReason.APPEAL_NOT_VIABLE,
            note="",
            now=NOW,
        )
    with pytest.raises(StateTransitionError):
        draft(item)


def test_the_workbench_shows_the_appeal(api: TestClient, root: Path) -> None:  # noqa: F811
    item = denied(root)
    view = draft(item)
    body = api.get(
        f"/api/v1/denials/{item}", headers=bearer(CLIENT_A, role=Role.DENIAL_SPECIALIST)
    ).json()
    assert body["appeal"]["appeal_id"] == str(view.appeal_id)
    assert body["appeal"]["status"] == "DRAFT"
    assert body["appeal"]["letter"] == view.letter


@pytest.mark.invariant
def test_signing_through_the_api_twice_sends_once(api: TestClient, root: Path) -> None:  # noqa: F811
    """PD-14."""
    item = denied(root)
    headers = bearer(CLIENT_A, role=Role.DENIAL_SPECIALIST)
    drafted = api.post(
        f"/api/v1/denials/{item}/appeal",
        json={},
        headers={**headers, "Idempotency-Key": f"ap-{uuid.uuid4()}"},
    ).json()
    revised = api.post(
        f"/api/v1/appeals/{drafted['appeal_id']}/versions",
        json={"letter": REVIEWED},
        headers={**headers, "Idempotency-Key": f"ap-{uuid.uuid4()}"},
    ).json()
    key = f"ap-{uuid.uuid4()}"
    path = f"/api/v1/appeals/{drafted['appeal_id']}/sign"
    body = {"version": revised["version"]}
    first = api.post(path, json=body, headers={**headers, "Idempotency-Key": key})
    again = api.post(path, json=body, headers={**headers, "Idempotency-Key": key})
    assert first.status_code == again.status_code == 200
    assert first.json()["status"] == "SENT"
    with session_scope() as session:
        assert len(session.scalars(select(AppealSignoff)).all()) == 1


@pytest.mark.parametrize(
    "left",
    [
        "Dear payer,\n[State why the denial\nshould be reversed]\nThanks",
        "Dear payer, State why the denial should be reversed] Thanks",
        "Dear payer, [ please reconsider. Thanks",
        "Dear payer, \uff3bState why\uff3d Thanks",
    ],
)
def test_a_half_edited_prompt_cannot_be_signed(root: Path, left: str) -> None:
    """UR-09: a prompt broken over lines, a lone bracket or a full-width one
    is still an unreviewed prompt."""
    view = draft(denied(root))
    revised = revise(view.appeal_id, left)
    with pytest.raises(ValidationFailedError):
        sign(revised.appeal_id, revised.version)


def test_no_appeal_is_sent_while_a_write_off_waits(root: Path) -> None:
    """One decision at a time: sending would close the denial and strand the
    write-off waiting for its approver."""
    item = denied(root)
    view = draft(item)
    reviewed = revise(view.appeal_id, REVIEWED)
    # A draft open blocks a write-off request outright ...
    with session_scope() as session, pytest.raises(StateTransitionError):
        dispositions.request_write_off(
            session,
            principal=SPECIALIST,
            work_item_id=item,
            reason=dispositions.WriteOffReason.APPEAL_NOT_VIABLE,
            note="",
            now=NOW,
        )
    # ... and the appeal is still signable.
    assert sign(reviewed.appeal_id, reviewed.version).status == "SENT"
