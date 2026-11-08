"""Correct and resubmit a denied claim (M5.7 slice 3, UR-10, PD-7, ADR-0018).

UR-10: the operator sees a field-level diff before anything changes. The
replacement is the original 837 read back from RAW (PD-9) with only the
edited fields changed, sent as a frequency-7 replacement chained to the
claim it corrects (PD-7). Charges are never edited here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from rcm.domain.claim import Claim, ClaimLine
from rcm.domain.claim_state import ClaimState
from rcm.platform.auth import Principal, Role
from rcm.platform.config import get_settings
from rcm.platform.db import session_scope
from rcm.platform.errors import AuthorizationDeniedError, ValidationFailedError
from rcm.submission import corrections
from rcm.submission.corrections import Correction, LineCorrection
from rcm.submission.models import SubmissionOutbox
from rcm.work.models import WorkItem
from tests.integration.test_acknowledgement_loading import CLIENT_A, ack_277ca, acknowledge
from tests.integration.test_api_shell import api, bearer, clean  # noqa: F401
from tests.integration.test_claim_progression import advance
from tests.integration.test_denial_classification import NOW
from tests.integration.test_denial_write_off import SPECIALIST, denied, person
from tests.integration.test_rules_engine import create, live, publish, rule_doc

pytestmark = pytest.mark.integration


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


_RULE_TABLES = "ruleset_member, ruleset, rule_version"


@pytest.fixture(autouse=True)
def ruleset(owner_engine: Engine) -> Iterator[None]:
    """A published ruleset with no rules: the replacement is scrubbed, and
    no ruleset at all is an error, never a pass (PD-18)."""

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {_RULE_TABLES} RESTART IDENTITY CASCADE"))

    wipe()
    publish()
    yield
    wipe()


def preview(item: uuid.UUID, change: Correction, root: Path) -> list[corrections.Change]:
    with session_scope() as session:
        return corrections.preview(
            session, principal=SPECIALIST, work_item_id=item, correction=change, archive_root=root
        )


def resubmit(
    item: uuid.UUID, change: Correction, root: Path, who: Principal = SPECIALIST
) -> corrections.Resubmitted:
    with session_scope() as session:
        return corrections.resubmit(
            session,
            principal=who,
            work_item_id=item,
            correction=change,
            archive_root=root,
            now=NOW,
        )


NEW_MEMBER = Correction(member_id="NEWMEMBER77")


@pytest.mark.invariant
def test_the_preview_shows_each_changed_field_and_changes_nothing(root: Path) -> None:
    """UR-10: a field-level diff before commit; the preview itself writes
    nothing."""
    item = denied(root)
    change = Correction(
        member_id="NEWMEMBER77",
        lines=(LineCorrection(line_number=1, modifiers=("GT",)),),
    )
    diff = preview(item, change, root)
    assert [(c.field, c.after) for c in diff] == [
        ("member_id", "••••ER77"),  # S-6: masked
        ("line 1 modifiers", "GT"),
    ]
    assert all(c.before != c.after for c in diff)
    with session_scope() as session:
        claims = session.scalars(select(Claim)).all()
    assert len(claims) == 1
    assert claims[0].state == str(ClaimState.DENIED)


def test_a_correction_that_changes_nothing_is_refused(root: Path) -> None:
    item = denied(root)
    with session_scope() as session:
        current = session.scalar(select(Claim.subscriber_member_id))
    assert current is not None
    with pytest.raises(ValidationFailedError):
        preview(item, Correction(member_id=current), root)


@pytest.mark.invariant
def test_resubmission_chains_to_original_from_the_workbench(root: Path) -> None:
    """PD-7: the corrected claim is the next submission of the same claim,
    frequency 7, carrying only the edit; the original waits in Correcting."""
    item = denied(root)
    done = resubmit(item, NEW_MEMBER, root)
    with session_scope() as session:
        original = session.scalar(select(Claim).where(Claim.submitter_claim_id == "CLM-WO"))
        replacement = session.get(Claim, done.claim_id)
        assert original is not None and replacement is not None
        assert replacement.original_claim_id == original.claim_id
        assert replacement.root_claim_id == original.root_claim_id
        assert replacement.submission_attempt == 2
        assert replacement.claim_frequency_code == "7"
        assert replacement.subscriber_member_id == "NEWMEMBER77"
        assert replacement.total_charge == original.total_charge  # charges never edited
        assert original.state == str(ClaimState.CORRECTING)
        found = session.get(WorkItem, item)
        assert found is not None
        assert (found.state, found.outcome) == ("Resolved", "CORRECTED_AND_RESUBMITTED")


def test_unchanged_lines_keep_their_codes_and_charges(root: Path) -> None:
    item = denied(root)
    done = resubmit(
        item, Correction(lines=(LineCorrection(line_number=1, units=Decimal("2")),)), root
    )
    with session_scope() as session:
        lines = session.scalars(select(ClaimLine).where(ClaimLine.claim_id == done.claim_id)).all()
    (line,) = lines
    assert (line.procedure_code, line.units, line.charge_amount) == (
        "99203",
        Decimal("2"),
        Decimal("200.00"),
    )


@pytest.mark.parametrize(
    "bad",
    [
        Correction(lines=(LineCorrection(line_number=9, units=Decimal("1")),)),
        Correction(lines=(LineCorrection(line_number=1, procedure_code="9920"),)),
        Correction(lines=(LineCorrection(line_number=1, modifiers=("A", "B", "C", "D", "E")),)),
        Correction(lines=(LineCorrection(line_number=1, diagnosis_pointers=(9,)),)),
        Correction(lines=(LineCorrection(line_number=1, units=Decimal("0")),)),
        Correction(diagnoses=()),
    ],
)
def test_an_invalid_correction_is_refused_by_name(root: Path, bad: Correction) -> None:
    with pytest.raises(ValidationFailedError):
        preview(denied(root), bad, root)


@pytest.mark.invariant
def test_a_replacement_the_scrubber_blocks_is_not_sent(root: Path) -> None:
    """PD-18: the replacement is scrubbed before it is sent. A block refuses
    the whole correction, by name, and nothing moves."""
    item = denied(root)
    live("NO_MOD25_ON_EM", create(rule_doc()))
    publish()
    with pytest.raises(ValidationFailedError) as refused:
        resubmit(item, NEW_MEMBER, root)
    assert "modifier 25" in refused.value.context["reason"]
    with session_scope() as session:
        claims = session.scalars(select(Claim)).all()
        found = session.get(WorkItem, item)
    assert [c.state for c in claims] == [str(ClaimState.DENIED)]
    assert found is not None and found.outcome is None


def test_the_replacement_is_queued_to_send(root: Path) -> None:
    """IR-06: Submitted only with its outbox row, in one transaction."""
    done = resubmit(denied(root), NEW_MEMBER, root)
    with session_scope() as session:
        replacement = session.get(Claim, done.claim_id)
        queued = session.scalars(
            select(SubmissionOutbox).where(SubmissionOutbox.claim_id == done.claim_id)
        ).all()
    assert replacement is not None and replacement.state == str(ClaimState.SUBMITTED)
    assert done.submitter_claim_id == "CLM-WO-R2"
    (row,) = queued
    assert b"REF*F8*CLM-WO~" in row.payload
    assert b"NEWMEMBER77" in row.payload


def test_the_original_is_replaced_once_the_payer_takes_the_replacement(root: Path) -> None:
    """ADR-0006: until the payer accepts the replacement the original stays
    the live claim; after, progression ends it as Replaced."""
    done = resubmit(denied(root), NEW_MEMBER, root)
    advance()
    with session_scope() as session:
        assert session.scalar(
            select(Claim.state).where(Claim.submitter_claim_id == "CLM-WO")
        ) == str(ClaimState.CORRECTING)
    acknowledge(ack_277ca((done.submitter_claim_id, "A2:20")), root, name="277ca-r2.edi")
    advance()
    with session_scope() as session:
        original = session.scalar(select(Claim).where(Claim.submitter_claim_id == "CLM-WO"))
        replacement = session.get(Claim, done.claim_id)
    assert original is not None and replacement is not None
    assert original.state == str(ClaimState.REPLACED)
    assert replacement.state == str(ClaimState.IN_ADJUDICATION)


def test_resubmitting_needs_denial_disposition(root: Path) -> None:
    with pytest.raises(AuthorizationDeniedError):
        resubmit(denied(root), NEW_MEMBER, root, who=person("ar.amy", Role.AR_SPECIALIST))


# --- through the API -------------------------------------------------------


@pytest.fixture
def archive(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RCM_RAW_ARCHIVE_ROOT", str(root))
    get_settings.cache_clear()
    return root


def post(api: TestClient, path: str, body: dict[str, Any], key: str) -> Any:  # noqa: F811
    headers = {**bearer(CLIENT_A, role=Role.DENIAL_SPECIALIST), "Idempotency-Key": key}
    return api.post(path, json=body, headers=headers)


@pytest.mark.invariant
def test_resubmitting_twice_with_one_key_resubmits_once(
    api: TestClient,  # noqa: F811
    archive: Path,
) -> None:
    """PD-14: a retried click is not a second replacement claim."""
    item = denied(archive)
    body = {"member_id": "NEWMEMBER77"}
    diff = post(api, f"/api/v1/denials/{item}/correction/preview", body, f"c-{uuid.uuid4()}")
    assert diff.status_code == 200
    assert diff.json()["changes"][0]["field"] == "member_id"
    # S-6 / PD-13: neither member ID leaves the server in full.
    assert "NEWMEMBER77" not in diff.text and "VAZ957419823" not in diff.text
    key = f"c-{uuid.uuid4()}"
    first = post(api, f"/api/v1/denials/{item}/correction", body, key)
    again = post(api, f"/api/v1/denials/{item}/correction", body, key)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    with session_scope() as session:
        assert len(session.scalars(select(Claim)).all()) == 2
