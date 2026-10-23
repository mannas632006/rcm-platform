"""The whole synthetic corpus, both sides, and the number it produces (M1.9).

This is where M1.9's acceptance condition is measured rather than reasoned
about. Both transaction types are loaded under one client -- 325 claim files
and 211 remittances -- and the question is how many claims the platform then
believes it has, and how often they were denied.

    submissions   616        denied submissions   107        17.37%
    claims        600        denied claims         91        15.17%

The gap is the whole point of BR-DATA-03. Both numbers are honest counts of
real rows; only one of them is the denial rate.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text

from rcm.analytics.denials import denial_rate
from rcm.analytics.reversals import reopened_claims
from rcm.domain.claim import Claim, ClaimLine
from rcm.domain.payer import Payer, PayerAlias
from rcm.domain.payer_registry import pending_matches, propose_matches
from rcm.ingestion.claim_loader import ClaimLoadOutcome, load_claim_file
from rcm.ingestion.remittance_loader import load_remittance_file
from rcm.platform.db import reset_engines, session_scope

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_CONFIG = REPO_ROOT / "tools" / "synthetic" / "config" / "smoke.yaml"
CLIENT = uuid.UUID("eeeeeeee-0000-4000-8000-00000000000e")


@pytest.fixture(scope="module")
def corpus() -> Path:
    from tools.synthetic.generate import OUT

    if OUT.exists():
        shutil.rmtree(OUT)
    result = subprocess.run(
        [sys.executable, "-m", "tools.synthetic", "--config", str(SMOKE_CONFIG)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"generator failed:\n{result.stdout}\n{result.stderr}"
    return OUT


@pytest.fixture(scope="module")
def loaded(
    corpus: Path, owner_engine: Engine, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[dict[str, object]]:
    """Load every claim, then every remittance, under one client.

    Claims first, because a remittance is an answer to a claim. The order is
    not required by the loaders -- they do not join to each other -- but
    loading them the way they actually arrive is the only way this fixture
    resembles the thing it stands in for.
    """
    reset_engines()
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE claim_line, claim, remittance_adjustment, "
                "remittance_service_line, claim_payment, provider_adjustment, "
                "remittance, raw_file, payer_alias, payer RESTART IDENTITY CASCADE"
            )
        )

    archive_root = tmp_path_factory.mktemp("raw")
    claim_files = sorted((corpus / "x12" / "837").glob("*.txt"))
    remittance_files = sorted((corpus / "x12" / "835").glob("*.txt"))
    assert claim_files and remittance_files

    outcomes: dict[str, int] = {}
    chained = 0
    for path in claim_files:
        with session_scope() as session:
            result = load_claim_file(
                session,
                path.read_bytes(),
                client_id=CLIENT,
                original_filename=path.name,
                archive_root=archive_root,
            )
        outcomes[result.outcome.value] = outcomes.get(result.outcome.value, 0) + 1
        chained += result.resubmissions_chained

    quarantined_remittances = 0
    for path in remittance_files:
        with session_scope() as session:
            remittance = load_remittance_file(
                session,
                path.read_bytes(),
                client_id=CLIENT,
                original_filename=path.name,
                archive_root=archive_root,
            )
        if remittance.quarantined:
            quarantined_remittances += 1

    yield {
        "claim_files": len(claim_files),
        "outcomes": outcomes,
        "chained": chained,
        "quarantined_remittances": quarantined_remittances,
        "archive_root": archive_root,
        "manifest": json.loads((corpus / "manifest.json").read_text(encoding="utf-8")),
    }
    reset_engines()


def _counts(key: str, loaded: dict[str, object]) -> int:
    manifest = loaded["manifest"]
    assert isinstance(manifest, dict)
    value = manifest["counts"][key]
    assert isinstance(value, int)
    return value


def test_every_claim_file_loaded(loaded: dict[str, object]) -> None:
    """No quarantines. Every assertion the loader makes holds on real files.

    Worth stating as its own test: IR-05, IR-07 and IR-08 would each quarantine
    a file, and a corpus that tripped one of them would make every count below
    quietly smaller.
    """
    assert loaded["outcomes"] == {ClaimLoadOutcome.LOADED.value: loaded["claim_files"]}


@pytest.mark.invariant
def test_three_submissions_count_as_one_claim_at_corpus_volume(
    loaded: dict[str, object],
) -> None:
    """616 submissions are 600 claims (BR-DATA-03)."""
    with session_scope() as session:
        submissions = session.scalar(select(func.count()).select_from(Claim))
        claims = session.scalar(select(func.count(func.distinct(Claim.root_claim_id))))
        resubmissions = session.scalar(
            select(func.count()).select_from(Claim).where(Claim.original_claim_id.isnot(None))
        )

    assert submissions == _counts("claims", loaded)
    assert claims == _counts("original_claims", loaded)
    assert resubmissions == _counts("resubmissions", loaded)
    assert loaded["chained"] == resubmissions
    # The arithmetic that makes the three agree.
    assert submissions == claims + resubmissions


def test_every_service_line_was_loaded(loaded: dict[str, object]) -> None:
    with session_scope() as session:
        lines = session.scalar(select(func.count()).select_from(ClaimLine))
    assert lines == _counts("service_lines", loaded)


def test_every_chain_resolves_to_a_real_claim(loaded: dict[str, object]) -> None:
    """A root pointing at nothing, or at a resubmission, would make the
    distinct count above meaningless."""
    del loaded
    with session_scope() as session:
        orphan_roots = session.execute(
            text(
                "SELECT count(*) FROM claim c "
                "WHERE NOT EXISTS (SELECT 1 FROM claim r WHERE r.claim_id = c.root_claim_id)"
            )
        ).scalar()
        # Every root is itself an original claim: roots never point at a
        # resubmission, which is what makes the chain one level deep however
        # many times a claim is resent.
        roots_that_are_resubmissions = session.execute(
            text(
                "SELECT count(*) FROM claim c JOIN claim r ON r.claim_id = c.root_claim_id "
                "WHERE r.original_claim_id IS NOT NULL"
            )
        ).scalar()

    assert orphan_roots == 0
    assert roots_that_are_resubmissions == 0


@pytest.mark.invariant
def test_denial_rate_excludes_resubmissions_at_corpus_volume(
    loaded: dict[str, object],
) -> None:
    """The measurement BR-DATA-03 exists to protect.

    One remittance in the corpus is the deliberately unbalanced file, which is
    quarantined and contributes nothing -- so the claims it would have
    adjudicated are simply not counted, in either figure.
    """
    assert loaded["quarantined_remittances"] == 1

    with session_scope() as session:
        result = denial_rate(session, client_id=CLIENT)

    assert result.submissions == 616
    assert result.denied_submissions == 107
    assert result.per_submission == Decimal("17.37")

    assert result.claims == 600
    assert result.denied_claims == 91
    assert result.rate == Decimal("15.17")

    # Counting submissions overstates the denial rate by 2.2 points, a 14%
    # relative error, in the direction that makes the operation look worse.
    assert result.inflation == Decimal("2.20")


def test_reprocessing_every_claim_file_changes_nothing(loaded: dict[str, object]) -> None:
    """PD-14 at corpus volume."""
    archive_root = loaded["archive_root"]
    assert isinstance(archive_root, Path)

    with session_scope() as session:
        before = (
            session.scalar(select(func.count()).select_from(Claim)),
            session.scalar(select(func.count()).select_from(ClaimLine)),
            session.scalar(select(func.count(func.distinct(Claim.root_claim_id)))),
        )

    from tools.synthetic.generate import OUT

    reloaded: dict[str, int] = {}
    for path in sorted((OUT / "x12" / "837").glob("*.txt")):
        with session_scope() as session:
            result = load_claim_file(
                session,
                path.read_bytes(),
                client_id=CLIENT,
                original_filename=path.name,
                archive_root=archive_root,
            )
        reloaded[result.outcome.value] = reloaded.get(result.outcome.value, 0) + 1

    assert reloaded == {ClaimLoadOutcome.ALREADY_LOADED.value: loaded["claim_files"]}

    with session_scope() as session:
        after = (
            session.scalar(select(func.count()).select_from(Claim)),
            session.scalar(select(func.count()).select_from(ClaimLine)),
            session.scalar(select(func.count(func.distinct(Claim.root_claim_id)))),
        )
    assert after == before


def test_every_claim_is_keyed_on_a_payer_identifier(loaded: dict[str, object]) -> None:
    """PD-8 on the claim side: identity on NM1*PR element 9, never the name."""
    del loaded
    with session_scope() as session:
        payers = session.execute(
            text("SELECT count(DISTINCT payer_id) FROM claim WHERE client_id = :c"),
            {"c": CLIENT},
        ).scalar()
        unknown = session.execute(
            text("SELECT count(*) FROM claim WHERE payer_id = 'UNKNOWN'")
        ).scalar()

    assert payers is not None
    assert payers > 1
    # Every claim named a payer, so nothing fell back to the placeholder.
    assert unknown == 0


@pytest.mark.invariant
def test_reversal_reopens_original_claim_at_corpus_volume(
    loaded: dict[str, object],
) -> None:
    """BR-M9-04 across the whole corpus.

    The reversal files were added to the generator for this milestone: the
    corpus had none, so every reversal test before this was a fixture test.
    """
    reversed_expected = _counts("reversed_claims", loaded)
    recouped_expected = _counts("recouped_claims", loaded)

    with session_scope() as session:
        summary = reopened_claims(session, client_id=CLIENT)

        postings = session.execute(
            text(
                "SELECT count(*) FILTER (WHERE is_reversal), "
                "       count(*) FILTER (WHERE is_reversal "
                "                        AND reverses_claim_payment_id IS NOT NULL) "
                "FROM claim_payment WHERE client_id = :c"
            ),
            {"c": CLIENT},
        ).one()
        recoupments = session.execute(
            text(
                "SELECT count(*) FILTER (WHERE reason_code = 'WO'), "
                "       count(*) FILTER (WHERE recoups_claim_payment_id IS NOT NULL) "
                "FROM provider_adjustment WHERE client_id = :c"
            ),
            {"c": CLIENT},
        ).one()
        negative_claims = session.execute(
            text("SELECT count(*) FROM claim WHERE client_id = :c AND total_charge < 0"),
            {"c": CLIENT},
        ).scalar()

    # Every reversal in the corpus is loaded and linked to what it reverses.
    assert postings[0] == reversed_expected
    assert postings[1] == reversed_expected
    # Every recoupment names a claim and is linked to its payment.
    assert recoupments[0] == recouped_expected
    assert recoupments[1] == recouped_expected

    # BR-M9-04: not a new negative claim. Not one.
    assert negative_claims == 0

    assert summary.reversed_claims == reversed_expected
    assert summary.recouped_claims == recouped_expected
    assert summary.total == reversed_expected + recouped_expected


def test_reversals_do_not_change_the_claim_count(loaded: dict[str, object]) -> None:
    """The counts M1.9 established are untouched by money moving backwards."""
    with session_scope() as session:
        submissions = session.scalar(select(func.count()).select_from(Claim))
        claims = session.scalar(select(func.count(func.distinct(Claim.root_claim_id))))

    assert submissions == _counts("claims", loaded)
    assert claims == _counts("original_claims", loaded)


def test_every_payer_spelling_in_the_corpus_is_recorded(loaded: dict[str, object]) -> None:
    """M1.7 at volume: the names the files actually used are all in the table.

    The corpus carried one canonical name per payer until this milestone, so
    alias resolution had nothing to resolve. It now carries several spellings
    per payer under the same identifier, which is what the acceptance
    condition describes.
    """
    del loaded
    with session_scope() as session:
        aliases = session.scalars(select(PayerAlias)).all()

    assert len(aliases) > 8  # more spellings than payers, which is the point
    # Every one unconfirmed: recording a spelling is an observation, and
    # nothing in the loader is entitled to confirm it.
    assert all(not alias.confirmed for alias in aliases)
    assert all(alias.normalised_alias == alias.normalised_alias.strip() for alias in aliases)


@pytest.mark.invariant
def test_the_two_identifier_namespaces_are_raised_for_review(
    loaded: dict[str, object],
) -> None:
    """The problem M1.7 exists for, measured.

    An 837 names a payer by its payer ID and the 835 answering it names the
    same payer by the clearinghouse ETIN, so loading both sides of the corpus
    produces two rows for every payer -- each with its own denial rate, each
    internally consistent, and all of them wrong.

    The expected count comes from the generator's reference data rather than
    from the loaded rows, so this measures the loader against an independent
    source instead of against itself.

    Counting *distinct names* would not work, and the reason is the point of
    PD-8: a payer's recorded name is whichever spelling arrived first, so the
    ETIN row for BCBS Texas is called `BLUE CROSS TEXAS` while the payer-ID
    row is called `BCBS Texas`. Sixteen rows, nine names, eight payers. Names
    do not count anything reliably, which is why they are not the key.
    """
    del loaded
    reference = REPO_ROOT / "tools" / "synthetic" / "reference" / "payers.csv"
    with reference.open(encoding="utf-8") as handle:
        real_payers = len(list(csv.DictReader(handle)))

    with session_scope() as session:
        payers = session.execute(text("SELECT count(*) FROM payer")).scalar()
        queue = pending_matches(session)

    # One row per identifier namespace, for every real payer.
    assert payers == 2 * real_payers
    # Every pair raised, and nothing merged: PD-8 leaves identity to a human.
    assert len(queue) == real_payers
    assert {c.payer_id for c in queue} | {c.other_payer_id for c in queue} == set(
        session_payer_ids()
    )
    assert all(c.payer_id < c.other_payer_id for c in queue)
    assert all(c.status == "PENDING" for c in queue)


def session_payer_ids() -> set[str]:
    with session_scope() as session:
        return set(session.scalars(select(Payer.payer_id)).all())


def test_rescanning_raises_nothing_new(loaded: dict[str, object]) -> None:
    """The conflicts were caught as the files were loaded, so a later scan has
    nothing left to find. A scan that keeps raising the same pairs is a queue
    nobody reads."""
    del loaded
    with session_scope() as session:
        assert propose_matches(session) == 0
