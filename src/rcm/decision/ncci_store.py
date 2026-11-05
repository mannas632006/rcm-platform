"""Load NCCI releases, and read the edits a claim needs (M4.3, ADR-0008)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from rcm.decision.ncci import MueLimit, NcciCheck, PtpEdit, parse_mue, parse_ptp
from rcm.decision.ncci_models import NcciMue, NcciPtpEdit, NcciRelease
from rcm.platform.errors import ValidationFailedError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

KINDS: Final = frozenset({"PTP", "MUE"})
_LABEL: Final = re.compile(r"[0-9A-Z][0-9A-Z_]{0,31}")


@dataclass(frozen=True, slots=True)
class LoadedRelease:
    release_id: str
    row_count: int
    #: False: this exact file was already loaded, and nothing changed.
    created: bool


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_release(
    session: Session, *, kind: str, label: str, payload: bytes, loaded_by: str
) -> LoadedRelease:
    """Load one CMS file whole, or not at all. Idempotent per file."""
    if kind not in KINDS:
        raise ValidationFailedError(field="kind", reason="PTP or MUE")
    if not _LABEL.fullmatch(label):
        raise ValidationFailedError(field="label", reason="e.g. 2026Q1")
    if not loaded_by.strip():
        raise ValidationFailedError(field="loaded_by", reason="name who loaded it")
    release_id = f"{kind}:{label}"
    digest = sha256(payload)

    existing = session.get(NcciRelease, release_id)
    if existing is not None:
        if existing.sha256 != digest:
            raise ValidationFailedError(
                field="label", reason=f"{release_id} was loaded from a different file"
            )
        return LoadedRelease(release_id, existing.row_count, created=False)

    # Parsed in full before anything is written: one bad row, nothing loads.
    rows: list[dict[str, object]]
    if kind == "PTP":
        rows = [
            {
                "release_id": release_id,
                "column_1": e.column_1,
                "column_2": e.column_2,
                "effective_date": e.effective,
                "deletion_date": e.deleted,
                "modifier_indicator": e.modifier_indicator,
            }
            for e in parse_ptp(payload)
        ]
    else:
        rows = [
            {
                "release_id": release_id,
                "code": m.code,
                "mue_value": m.units,
                "adjudication_indicator": m.adjudication_indicator,
            }
            for m in parse_mue(payload)
        ]

    session.add(
        NcciRelease(
            release_id=release_id,
            kind=kind,
            label=label,
            sha256=digest,
            row_count=len(rows),
            loaded_by=loaded_by,
        )
    )
    session.flush()
    session.execute(insert(NcciPtpEdit if kind == "PTP" else NcciMue), rows)
    log.info("ncci.release_loaded", release_id=release_id, rows=len(rows))
    return LoadedRelease(release_id, len(rows), created=True)


def latest_release(session: Session, kind: str) -> str | None:
    return session.scalar(
        select(NcciRelease.release_id)
        .where(NcciRelease.kind == kind)
        .order_by(NcciRelease.loaded_at.desc(), NcciRelease.release_id.desc())
        .limit(1)
    )


def check_for(
    session: Session,
    *,
    ptp_release: str | None,
    mue_release: str | None,
    codes: Collection[str],
) -> NcciCheck:
    """The edits of the pinned releases that touch these codes, and no more."""
    ptp: list[PtpEdit] = []
    mue: list[MueLimit] = []
    wanted = sorted(set(codes))
    if ptp_release is not None and wanted:
        ptp = [
            PtpEdit(r.column_1, r.column_2, r.effective_date, r.deletion_date, r.modifier_indicator)
            for r in session.scalars(
                select(NcciPtpEdit).where(
                    NcciPtpEdit.release_id == ptp_release,
                    NcciPtpEdit.column_1.in_(wanted),
                    NcciPtpEdit.column_2.in_(wanted),
                )
            )
        ]
    if mue_release is not None and wanted:
        mue = [
            MueLimit(r.code, r.mue_value, r.adjudication_indicator)
            for r in session.scalars(
                select(NcciMue).where(NcciMue.release_id == mue_release, NcciMue.code.in_(wanted))
            )
        ]
    return NcciCheck(ptp_release=ptp_release, mue_release=mue_release, ptp=ptp, mue=mue)
