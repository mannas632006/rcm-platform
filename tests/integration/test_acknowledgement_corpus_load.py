"""PD-4 at corpus volume (M1.6).

The unit and integration tests prove a rejection is not a denial on three
claims with three fates. This file asks the same question of the whole
corpus, because the failure mode PD-4 guards against is statistical: one
rejection counted as a denial is a wrong row, and several hundred are a denial
rate nobody can reconcile and nobody can detect, since every number involved
is a real count of real rows.

Three things are checked, and the first is the one that matters:

    no claim is both rejected and denied   the sets are disjoint, measured
    rejections exist at all                otherwise the first is vacuous
    the counts match the generator         which shares no code with the parser

The second is not padding. A corpus with no rejections in it would satisfy the
disjointness assertion perfectly while testing nothing, and that is exactly
the failure this suite found twice before -- zero multi-modifier lines in
M1.5, zero reversals in M1.10 -- so the corpus is required to contain the
shape, not merely permitted to.
"""

from __future__ import annotations

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
from rcm.domain.acknowledgement import AcknowledgementOutcome, AcknowledgementSource
from rcm.domain.acknowledgement import ClaimAcknowledgement as Ack
from rcm.ingestion.acknowledgement_loader import (
    AcknowledgementLoadOutcome,
    load_acknowledgement_file,
)
from rcm.ingestion.claim_loader import load_claim_file
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
    corpus: Path,
    owner_engine: Engine,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, object]]:
    """Claims, then acknowledgements, then remittances -- the real order.

    Loading acknowledgements before the 835s matters: it is the order the
    files arrive in, and it is the order in which a loader that wrote
    rejections into the remittance tables would corrupt the denial rate.
    """

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE claim_acknowledgement, claim_line, claim, "
                    "remittance_adjustment, remittance_service_line, claim_payment, "
                    "provider_adjustment, remittance, raw_file, payer_alias, payer "
                    "RESTART IDENTITY CASCADE"
                )
            )

    reset_engines()
    wipe()
    archive_root = tmp_path_factory.mktemp("raw")
    counts = {"claims": 0, "acknowledgements": 0, "remittances": 0, "quarantined": 0}

    for path in sorted((corpus / "x12" / "837").glob("*.txt")):
        with session_scope() as session:
            load_claim_file(
                session,
                path.read_bytes(),
                client_id=CLIENT,
                original_filename=path.name,
                archive_root=archive_root,
            )
        counts["claims"] += 1

    for folder in ("999", "277CA"):
        for path in sorted((corpus / "x12" / folder).glob("*.txt")):
            with session_scope() as session:
                result = load_acknowledgement_file(
                    session,
                    path.read_bytes(),
                    client_id=CLIENT,
                    original_filename=path.name,
                    archive_root=archive_root,
                )
            counts["acknowledgements"] += 1
            if result.outcome is AcknowledgementLoadOutcome.QUARANTINED:
                counts["quarantined"] += 1

    for path in sorted((corpus / "x12" / "835").glob("*.txt")):
        with session_scope() as session:
            load_remittance_file(
                session,
                path.read_bytes(),
                client_id=CLIENT,
                original_filename=path.name,
                archive_root=archive_root,
            )
        counts["remittances"] += 1

    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    yield {"counts": counts, "manifest": manifest, "corpus": corpus}
    wipe()


def _manifest_counts(loaded: dict[str, object]) -> dict[str, int]:
    manifest = loaded["manifest"]
    assert isinstance(manifest, dict)
    counts = manifest["counts"]
    assert isinstance(counts, dict)
    return counts


# ---------------------------------------------------------------------------
# The invariant, at volume
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_no_claim_is_both_rejected_and_denied(loaded: dict[str, object]) -> None:
    """PD-4 over the whole corpus: the two sets do not intersect.

    A claim returned by the payer's front end never reached adjudication, so
    there is no 835 for it and no denial to find. One claim in both sets means
    either the generator produced an impossible history or the loader wrote a
    rejection somewhere the denial query can see -- and both are bugs that
    would otherwise surface as an unexplainable denial rate.
    """
    with session_scope() as session:
        rejected = set(
            session.scalars(
                select(Ack.claim_id).where(
                    Ack.client_id == CLIENT,
                    Ack.outcome == str(AcknowledgementOutcome.REJECTED),
                    Ack.source == str(AcknowledgementSource.CLAIM_277CA),
                )
            ).all()
        )
        denied = set(
            session.execute(
                text(
                    "SELECT DISTINCT cp.claim_id FROM claim_payment cp "
                    "JOIN remittance_service_line sl "
                    "  ON sl.claim_payment_id = cp.claim_payment_id "
                    "JOIN remittance_adjustment ra "
                    "  ON ra.service_line_id = sl.service_line_id "
                    "WHERE cp.client_id = :client AND sl.paid_amount = 0"
                ),
                {"client": CLIENT},
            )
            .scalars()
            .all()
        )

    assert rejected, "corpus contains no 277CA rejections, so PD-4 is untested at volume"
    assert denied, "corpus contains no denials, so the disjointness proves nothing"
    assert not (rejected & denied), sorted(rejected & denied)[:10]


@pytest.mark.invariant
def test_rejections_do_not_reach_the_denial_rate(loaded: dict[str, object]) -> None:
    """The number itself, not just the row sets.

    The denominator is adjudicated claims. Rejected claims have no
    remittance, so they are absent from both halves of the fraction, and the
    rate is unchanged by how many of them there are.
    """
    manifest = _manifest_counts(loaded)
    never_adjudicated = manifest["claims_never_adjudicated"]
    assert never_adjudicated > 0

    with session_scope() as session:
        result = denial_rate(session, client_id=CLIENT)
        submitted = session.execute(
            text("SELECT count(*) FROM claim WHERE client_id = :c"), {"c": CLIENT}
        ).scalar_one()

    # The exact arithmetic, not an inequality: every submission the payer
    # rejected is absent from the adjudicated denominator, and the shortfall
    # is the generator's own count of them to the row.
    #
    #     submitted 616  -  never adjudicated 36  =  adjudicated 580
    #
    # An inequality would pass if the loader dropped claims for some
    # unrelated reason, which is the kind of agreement that looks like proof
    # and is not.
    assert result.submissions == submitted - never_adjudicated

    # And the rate is a real fraction of the adjudicated set, so neither half
    # has been silently emptied.
    assert Decimal(0) < result.rate < Decimal(100)
    assert result.denied_claims < result.claims


# ---------------------------------------------------------------------------
# The corpus actually contains the shapes
# ---------------------------------------------------------------------------


def test_every_acknowledgement_file_loaded(loaded: dict[str, object]) -> None:
    counts = loaded["counts"]
    assert isinstance(counts, dict)
    assert counts["acknowledgements"] > 0
    assert counts["quarantined"] == 0


def test_the_loaded_verdicts_match_the_generator(loaded: dict[str, object]) -> None:
    """Reconciled against `manifest.json`, which the generator writes and the
    parser never reads -- so agreement is two independent implementations
    arriving at the same number rather than one asserting itself."""
    manifest = _manifest_counts(loaded)

    with session_scope() as session:
        accepted_277 = session.scalar(
            select(func.count())
            .select_from(Ack)
            .where(
                Ack.client_id == CLIENT,
                Ack.source == str(AcknowledgementSource.CLAIM_277CA),
                Ack.outcome == str(AcknowledgementOutcome.ACCEPTED),
            )
        )
        rejected_277 = session.scalar(
            select(func.count())
            .select_from(Ack)
            .where(
                Ack.client_id == CLIENT,
                Ack.source == str(AcknowledgementSource.CLAIM_277CA),
                Ack.outcome == str(AcknowledgementOutcome.REJECTED),
            )
        )
        rejected_999 = session.scalar(
            select(func.count())
            .select_from(Ack)
            .where(
                Ack.client_id == CLIENT,
                Ack.source == str(AcknowledgementSource.SYNTAX_999),
                Ack.outcome == str(AcknowledgementOutcome.REJECTED),
            )
        )

    assert accepted_277 == manifest["accepted_277ca_claims"]
    assert rejected_277 == manifest["rejected_277ca_claims"]
    assert rejected_999 == manifest["rejected_999"]


def test_the_corpus_contains_both_kinds_of_rejection(loaded: dict[str, object]) -> None:
    """A 999 rejection and a 277CA rejection are different events with
    different consequences (BR-M8-02), so a corpus carrying only one of them
    leaves half the distinction unexercised."""
    manifest = _manifest_counts(loaded)
    assert manifest["rejected_999"] > 0, "no syntax rejections in the corpus"
    assert manifest["rejected_277ca_claims"] > 0, "no front-end rejections in the corpus"


@pytest.mark.invariant
def test_reprocessing_the_acknowledgement_corpus_changes_nothing(
    loaded: dict[str, object],
) -> None:
    """PD-14 across the whole set, which is the gate M1 is measured by."""
    corpus = loaded["corpus"]
    assert isinstance(corpus, Path)

    def snapshot() -> list[tuple[str, str, str, str]]:
        with session_scope() as session:
            return [
                (r.claim_id, r.source, r.outcome, r.category_code)
                for r in session.scalars(
                    select(Ack)
                    .where(Ack.client_id == CLIENT)
                    .order_by(Ack.claim_id, Ack.source, Ack.category_code)
                ).all()
            ]

    before = snapshot()
    archive_root = corpus.parent / "raw-replay"
    archive_root.mkdir(exist_ok=True)

    for folder in ("999", "277CA"):
        for path in sorted((corpus / "x12" / folder).glob("*.txt")):
            with session_scope() as session:
                result = load_acknowledgement_file(
                    session,
                    path.read_bytes(),
                    client_id=CLIENT,
                    original_filename=path.name,
                    archive_root=archive_root,
                )
            assert result.outcome is AcknowledgementLoadOutcome.ALREADY_LOADED

    assert snapshot() == before
