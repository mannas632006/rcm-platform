"""Where a model's training rows came from, read from the files (ADR-0026).

CLAUDE.md section 8: synthetic data must not train a production model, and
`tools/synthetic` asks this subsystem to enforce it rather than trust a flag.

Every claim and every remittance keeps the raw file it was loaded from
(PD-9), and every X12 interchange says what it is: ISA15 is `P` for
production and `T` for test. The generator writes `T`, as any sender of test
traffic must. So the data source is not an argument anyone passes: a model
is REAL only when every 837 and 835 behind its rows is a production
interchange. One test file makes it SYNTHETIC, and the registry will not
load a SYNTHETIC model in production.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.decision.ml.registry_models import DataSource
from rcm.domain.claim import Claim
from rcm.domain.remittance import ClaimPayment, RawFile, Remittance
from rcm.ingestion.archive import read_archived
from rcm.ingestion.x12 import read_interchange
from rcm.platform.errors import ValidationFailedError

_CHUNK: Final = 5000


def _chunks(ids: Collection[uuid.UUID]) -> Iterator[list[uuid.UUID]]:
    ordered = sorted(ids)
    for start in range(0, len(ordered), _CHUNK):
        yield ordered[start : start + _CHUNK]


def _files(session: Session, claim_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
    files: set[uuid.UUID] = set()
    for chunk in _chunks(claim_ids):
        files.update(
            f
            for f in session.scalars(select(Claim.raw_file_id).where(Claim.claim_id.in_(chunk)))
            if f is not None
        )
        # A payment names the claim as the payer echoed it (CLP01): the
        # submitter's claim id, within the same client.
        files.update(
            session.scalars(
                select(Remittance.raw_file_id)
                .join(ClaimPayment, ClaimPayment.remittance_id == Remittance.remittance_id)
                .join(
                    Claim,
                    (Claim.client_id == ClaimPayment.client_id)
                    & (Claim.submitter_claim_id == ClaimPayment.claim_id),
                )
                .where(Claim.claim_id.in_(chunk))
            )
        )
    return files


def data_source(
    session: Session, *, claim_ids: Collection[uuid.UUID], archive_root: Path | str
) -> DataSource:
    """REAL only if every interchange behind these claims is a production one."""
    files = _files(session, claim_ids)
    if not files:
        # Rows with no file behind them cannot vouch for themselves (PD-18).
        raise ValidationFailedError(field="dataset", reason="no raw file behind the rows")
    for chunk in _chunks(files):
        for raw in session.scalars(select(RawFile).where(RawFile.raw_file_id.in_(chunk))):
            interchange = read_interchange(
                read_archived(raw.storage_path, archive_root=archive_root)
            )
            if not interchange.is_production:
                return DataSource.SYNTHETIC
    return DataSource.REAL
