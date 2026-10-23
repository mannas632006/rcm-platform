"""Alias recording, resolution and the review queue (M1.7).

M1.7's acceptance condition names three spellings:

    "BCBS TX" / "Blue Cross Texas" / "BCBSTX" resolve to one payer_id

They resolve through the alias table, not through string similarity. Each was
seen on a file that named the same payer, so each is recorded against that
identifier; a matcher that tried to bridge `Blue Cross Texas` to `BCBSTX` on
text alone would need to know what the acronym stands for, and the only way to
make it appear to work is to lower the threshold until this one case passes.

The other half of these tests is the queue. PD-8 keys identity on `payer_id`,
so nothing here decides that two payers are one -- it decides that a human
should be asked.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, func, select, text

from rcm.domain.payer import MatchCandidateStatus, Payer, PayerAlias, PayerMatchCandidate
from rcm.domain.payer_registry import (
    confirm_alias,
    pending_matches,
    propose_matches,
    record_alias,
    resolve_by_name,
    review_match,
)
from rcm.platform.db import reset_engines, session_scope

pytestmark = pytest.mark.integration

BCBS = "00590"
BCBS_ETIN = "PAYER00590"
CIGNA = "62308"


@pytest.fixture(autouse=True)
def clean_registry(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE payer_match_candidate, claim_line, claim, "
                    "remittance_adjustment, remittance_service_line, claim_payment, "
                    "provider_adjustment, remittance, raw_file, payer_alias, payer "
                    "RESTART IDENTITY CASCADE"
                )
            )

    reset_engines()
    wipe()
    yield
    wipe()


def seed(session, payer_id: str, name: str) -> None:
    session.add(Payer(payer_id=payer_id, name=name))
    session.flush()


# ---------------------------------------------------------------------------
# The acceptance condition
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_several_spellings_resolve_to_one_payer_id() -> None:
    """M1.7, stated as CLAUDE.md states it.

    Every spelling arrived on a file naming payer 00590, so every spelling is
    recorded against 00590. Once a human confirms them, all three resolve.
    """
    spellings = ["BCBS TX", "Blue Cross Texas", "BCBSTX", "BCBS Texas", "bcbs  texas"]

    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        for spelling in spellings:
            record_alias(session, payer_id=BCBS, name=spelling)

    with session_scope() as session:
        # Unconfirmed, so nothing resolves yet. A suggestion is not an answer.
        assert all(resolve_by_name(session, spelling) is None for spelling in spellings)

        for alias in session.scalars(select(PayerAlias)).all():
            confirm_alias(
                session, normalised_alias=alias.normalised_alias, reviewed_by="user:reviewer"
            )

    with session_scope() as session:
        resolved = {resolve_by_name(session, spelling) for spelling in spellings}
        assert resolved == {BCBS}


def test_spelling_noise_resolves_without_its_own_alias_row() -> None:
    """`BCBS TX` and `bcbs   tx.` normalise to the same key, so one row serves
    both. The alias table holds names, not every way of typing one."""
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        record_alias(session, payer_id=BCBS, name="BCBS TX")
        alias = session.scalars(select(PayerAlias)).one()
        confirm_alias(session, normalised_alias=alias.normalised_alias, reviewed_by="user:reviewer")

    with session_scope() as session:
        assert resolve_by_name(session, "bcbs   tx.") == BCBS
        assert session.scalar(select(func.count()).select_from(PayerAlias)) == 1


# ---------------------------------------------------------------------------
# PD-18: an unconfirmed alias is a suggestion
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_an_unconfirmed_alias_does_not_resolve() -> None:
    """Resolving through a suggestion is how two payers quietly become one.

    The alias is recorded -- the spelling was genuinely seen -- but recording
    what arrived and trusting it to attribute money are different acts.
    """
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        record_alias(session, payer_id=BCBS, name="BCBS TX")

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(PayerAlias)) == 1
        assert resolve_by_name(session, "BCBS TX") is None


def test_an_unknown_name_resolves_to_nothing_rather_than_the_closest() -> None:
    """PD-18: on uncertainty the system does not act."""
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        record_alias(session, payer_id=BCBS, name="BCBS Texas")
        confirm_alias(session, normalised_alias="bcbs texas", reviewed_by="user:reviewer")

    with session_scope() as session:
        assert resolve_by_name(session, "Some Payer Nobody Has Heard Of") is None
        assert resolve_by_name(session, "") is None


def test_an_empty_name_is_not_recorded() -> None:
    """An empty `N1*PR` is common and is not evidence of anything."""
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        assert record_alias(session, payer_id=BCBS, name="   ") is None
        assert record_alias(session, payer_id=BCBS, name="...") is None

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(PayerAlias)) == 0


# ---------------------------------------------------------------------------
# One spelling, two payers
# ---------------------------------------------------------------------------


def test_a_spelling_claimed_by_two_payers_raises_a_candidate() -> None:
    """The case the corpus already contains.

    An 837 names the payer `00590` and the 835 answering it names
    `PAYER00590`, because a clearinghouse ETIN and a payer ID are different
    identifiers from different namespaces. The same name arrives under both.
    """
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        seed(session, BCBS_ETIN, "BCBS Texas")
        first = record_alias(session, payer_id=BCBS, name="BCBS Texas")
        second = record_alias(session, payer_id=BCBS_ETIN, name="BCBS Texas")

        assert first is not None
        assert second is not None
        assert first.created is True
        assert second.conflicted is True
        # The first claim stands; the second does not overwrite it.
        assert second.payer_id == BCBS

    with session_scope() as session:
        # One alias row, because one normalised spelling resolves to one payer.
        assert session.scalar(select(func.count()).select_from(PayerAlias)) == 1
        queue = pending_matches(session)
        assert len(queue) == 1
        assert {queue[0].payer_id, queue[0].other_payer_id} == {BCBS, BCBS_ETIN}


def test_a_conflict_does_not_reassign_the_alias() -> None:
    """Whoever claimed the spelling keeps it until a human says otherwise.

    Letting the newest file win would make resolution depend on load order,
    which is the failure the uniqueness constraint exists to prevent.
    """
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        seed(session, BCBS_ETIN, "BCBS Texas")
        record_alias(session, payer_id=BCBS, name="BCBS Texas")
        record_alias(session, payer_id=BCBS_ETIN, name="BCBS Texas")
        confirm_alias(session, normalised_alias="bcbs texas", reviewed_by="user:reviewer")

    with session_scope() as session:
        assert resolve_by_name(session, "BCBS Texas") == BCBS


def test_the_same_pair_is_proposed_once() -> None:
    """A queue that re-raises what it already holds stops being read."""
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        seed(session, BCBS_ETIN, "BCBS Texas")
        for _ in range(3):
            record_alias(session, payer_id=BCBS, name="BCBS Texas")
            record_alias(session, payer_id=BCBS_ETIN, name="BCBS Texas")
        propose_matches(session)
        propose_matches(session)

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(PayerMatchCandidate)) == 1


# ---------------------------------------------------------------------------
# The scan, and what it refuses to propose
# ---------------------------------------------------------------------------


def test_the_scan_proposes_payers_whose_names_match() -> None:
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        seed(session, BCBS_ETIN, "bcbs  texas")
        assert propose_matches(session) == 1

    with session_scope() as session:
        candidate = pending_matches(session)[0]
        assert {candidate.payer_id, candidate.other_payer_id} == {BCBS, BCBS_ETIN}
        assert float(candidate.confidence) == 1.0


def test_the_scan_leaves_different_payers_alone() -> None:
    """The half that matters. A scan proposing everything is not a control."""
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        seed(session, CIGNA, "Cigna PPO")
        seed(session, "87726", "UHC Commercial")
        seed(session, "84980", "UHC Medicare Advantage")
        assert propose_matches(session) == 0

    with session_scope() as session:
        assert pending_matches(session) == ()


def test_a_rejected_candidate_is_not_proposed_again() -> None:
    """Re-asking a question a human has answered is how a queue becomes noise."""
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        seed(session, BCBS_ETIN, "BCBS Texas")
        propose_matches(session)
        candidate = pending_matches(session)[0]
        review_match(session, candidate, confirmed=False, reviewed_by="user:reviewer")

    with session_scope() as session:
        assert propose_matches(session) == 0
        assert pending_matches(session) == ()
        rejected = session.scalars(select(PayerMatchCandidate)).one()
        assert rejected.status == str(MatchCandidateStatus.REJECTED)
        assert rejected.reviewed_by == "user:reviewer"
        assert rejected.reviewed_at is not None


# ---------------------------------------------------------------------------
# PD-8
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_confirming_a_candidate_does_not_merge_the_payers() -> None:
    """A merge changes identity, and PD-8 puts identity out of reach of a
    heuristic. Confirming records that a person looked and said yes."""
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        seed(session, BCBS_ETIN, "BCBS Texas")
        propose_matches(session)
        candidate = pending_matches(session)[0]
        review_match(session, candidate, confirmed=True, reviewed_by="user:reviewer")

    with session_scope() as session:
        # Both payers still exist. Nothing was rewritten.
        assert session.scalar(select(func.count()).select_from(Payer)) == 2
        confirmed = session.scalars(select(PayerMatchCandidate)).one()
        assert confirmed.status == str(MatchCandidateStatus.CONFIRMED)


def test_a_payer_is_never_a_candidate_against_itself() -> None:
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        assert propose_matches(session) == 0

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(PayerMatchCandidate)) == 0


def test_a_candidate_records_the_evidence_not_just_a_score() -> None:
    """A reviewer deciding on a number alone is a reviewer rubber-stamping."""
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        seed(session, BCBS_ETIN, "BCBS Texas")
        propose_matches(session)

    with session_scope() as session:
        candidate = pending_matches(session)[0]
        assert candidate.matched_on == "BCBS Texas"
        assert 0 <= float(candidate.confidence) <= 1


def test_confirming_an_alias_nobody_recorded_does_nothing() -> None:
    """Returns False rather than creating the alias it was asked to confirm.

    Confirming is a decision about a spelling that was actually seen.
    Inventing the row would let a reviewer conjure a resolution for a name no
    payer ever sent.
    """
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        assert confirm_alias(session, normalised_alias="never seen", reviewed_by="u") is False

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(PayerAlias)) == 0


def test_a_spelling_seen_twice_for_one_payer_is_recorded_once() -> None:
    """The ordinary case: the same payer under the same name in many files."""
    with session_scope() as session:
        seed(session, BCBS, "BCBS Texas")
        first = record_alias(session, payer_id=BCBS, name="BCBS Texas")
        second = record_alias(session, payer_id=BCBS, name="bcbs  texas")

        assert first is not None
        assert second is not None
        assert first.created is True
        assert second.created is False
        assert second.conflicted is False

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(PayerAlias)) == 1
        # And no candidate: one payer agreeing with itself is not a duplicate.
        assert session.scalar(select(func.count()).select_from(PayerMatchCandidate)) == 0
