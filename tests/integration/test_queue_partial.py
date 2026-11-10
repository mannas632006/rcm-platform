"""A queue is worked whether or not the model answers (M5.12, UR-34, FR-ML-06).

The recovery model (ML-3) is optional to the queue: when it is missing,
fails, breaks its own database work or answers nonsense, the page still
loads in its rules order, each score reads as unavailable -- never zero --
and the page says why.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from rcm.decision import scoring
from rcm.domain.claim import Claim
from rcm.platform.db import session_scope
from rcm.work.models import WorkItemKind
from tests.integration.test_acknowledgement_loading import CLIENT_A, submit
from tests.integration.test_api_shell import api, clean, items, raise_item  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def no_model() -> Iterator[None]:
    scoring.deploy_recovery_model(None)
    yield
    scoring.deploy_recovery_model(None)


@pytest.fixture
def claims(tmp_path: Path) -> dict[str, uuid.UUID]:
    root = tmp_path / "raw"
    root.mkdir()
    submit("CLM-A", root, control="0001")
    submit("CLM-B", root, control="0002")
    with session_scope() as session:
        ids = dict(session.execute(select(Claim.submitter_claim_id, Claim.claim_id)).all())
    raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, "a", claim_id=ids["CLM-A"])
    raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, "b", claim_id=ids["CLM-B"])
    return ids


class Answers:
    def __init__(self, answer: object) -> None:
        self.answer = answer

    def score(
        self, session: Session, *, client_id: uuid.UUID, claim_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, float]:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer  # type: ignore[return-value]


class BreaksTheTransaction:
    def score(
        self, session: Session, *, client_id: uuid.UUID, claim_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, float]:
        session.execute(text("SELECT * FROM no_such_table"))
        return {}


def scores(page: dict[str, object]) -> dict[str, object]:
    rows = page["items"]
    assert isinstance(rows, list)
    return {str(row["submitter_claim_id"]): row["recovery_probability"] for row in rows}


def test_no_model_yet_reads_unavailable_not_zero(
    api: TestClient,  # noqa: F811
    claims: dict[str, uuid.UUID],
) -> None:
    page = items(api, "data-quality")
    assert scores(page) == {"CLM-A": None, "CLM-B": None}
    assert page["recovery_unavailable"] == scoring.NOT_DEPLOYED


@pytest.mark.invariant
def test_a_failing_model_never_blocks_the_queue(
    api: TestClient,  # noqa: F811
    claims: dict[str, uuid.UUID],
) -> None:
    """UR-34: the claims load and can be worked; the score is marked missing."""
    scoring.deploy_recovery_model(Answers(TimeoutError("model timed out for CLM-A")))
    page = items(api, "data-quality")
    assert scores(page) == {"CLM-A": None, "CLM-B": None}
    assert page["recovery_unavailable"] == scoring.FAILED
    # PD-13: the model's own error text (which may echo inputs) is not shown.
    assert "CLM-A" not in str(page["recovery_unavailable"])


@pytest.mark.invariant
@pytest.mark.parametrize("model", [None, "fails"])
def test_model_degradation_falls_back_to_rules(
    api: TestClient,  # noqa: F811
    claims: dict[str, uuid.UUID],
    model: str | None,
) -> None:
    """CLAUDE.md 7.2 / M5.12: with the model absent or failing, the queue is
    the rules' order -- the priority score -- and every item is still there.

    CLM-B is given the higher rules priority. Neither the model's silence
    nor its failure may move the queue off the rules (PD-5, PD-18).
    """
    with session_scope() as session:
        session.execute(
            text("UPDATE work_item SET priority_score = :p WHERE claim_id = :c"),
            [{"p": 10, "c": claims["CLM-A"]}, {"p": 90, "c": claims["CLM-B"]}],
        )
    if model == "fails":
        scoring.deploy_recovery_model(Answers(RuntimeError("model unreachable")))

    page = items(api, "data-quality")
    rows = page["items"]
    assert isinstance(rows, list)
    assert [row["submitter_claim_id"] for row in rows] == ["CLM-B", "CLM-A"]
    assert {row["recovery_probability"] for row in rows} == {None}
    assert page["recovery_unavailable"] == (
        scoring.FAILED if model == "fails" else scoring.NOT_DEPLOYED
    )


def test_a_model_that_breaks_its_database_work_does_not_take_the_page(
    api: TestClient,  # noqa: F811
    claims: dict[str, uuid.UUID],
) -> None:
    """The savepoint keeps the read's transaction usable."""
    scoring.deploy_recovery_model(BreaksTheTransaction())
    page = items(api, "data-quality")
    assert len(scores(page)) == 2
    assert page["recovery_unavailable"] == scoring.FAILED


def test_scores_appear_when_the_model_answers(
    api: TestClient,  # noqa: F811
    claims: dict[str, uuid.UUID],
) -> None:
    scoring.deploy_recovery_model(Answers({claims["CLM-A"]: 0.72, claims["CLM-B"]: 0.1}))
    page = items(api, "data-quality")
    assert scores(page) == {"CLM-A": 0.72, "CLM-B": 0.1}
    assert page["recovery_unavailable"] is None


@pytest.mark.parametrize("bad", [1.5, -0.1, float("nan"), True, "0.5"])
def test_an_answer_that_is_not_a_probability_is_unavailable(
    api: TestClient,  # noqa: F811
    claims: dict[str, uuid.UUID],
    bad: object,
) -> None:
    """FR-ML-06: never guessed, never clamped into range."""
    scoring.deploy_recovery_model(Answers({claims["CLM-A"]: bad, claims["CLM-B"]: 0.4}))
    assert scores(items(api, "data-quality")) == {"CLM-A": None, "CLM-B": 0.4}


def test_an_answer_that_is_not_a_mapping_is_a_failed_model(
    api: TestClient,  # noqa: F811
    claims: dict[str, uuid.UUID],
) -> None:
    scoring.deploy_recovery_model(Answers([0.5, 0.5]))
    page = items(api, "data-quality")
    assert page["recovery_unavailable"] == scoring.FAILED
    assert set(scores(page).values()) == {None}
