"""Denial classification, with every unclassified denial put before a person
(M4.8, ADR-0012).

`rcm.decision.denials` classifies and reports what the taxonomy does not
hold; it sits below this layer and cannot open work. This module runs it and
raises each unmapped line as a named DATA_QUALITY exception, so a refusal
code nobody mapped is never just a number in a log line (PD-18).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.decision import denials
from rcm.platform.audit import DecisionType, record_decision
from rcm.platform.errors import ValidationFailedError
from rcm.platform.logging import get_logger
from rcm.work import exceptions
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import OPEN_STATES

log = get_logger(__name__)

#: One item per line per taxonomy version: a new version that still does not
#: hold the code raises it afresh.
_UNMAPPED_KEY: Final = "denial-unmapped:"


def _source_key(line: denials.UnmappedLine) -> str:
    return f"{_UNMAPPED_KEY}{denials.TAXONOMY_VERSION}:{line.service_line_id}"


def _open(session: Session, *, client_id: uuid.UUID, source_key: str) -> bool:
    return (
        session.scalar(
            select(WorkItem.work_item_id)
            .where(
                WorkItem.client_id == client_id,
                WorkItem.kind == str(WorkItemKind.DATA_QUALITY),
                WorkItem.source_key == source_key,
                WorkItem.state.in_([str(s) for s in OPEN_STATES]),
            )
            .limit(1)
        )
        is not None
    )


def classify_and_route(
    session: Session, *, client_id: uuid.UUID, now: datetime
) -> denials.ClassificationReport:
    """Classify one client's denials; raise each unmapped one with a person.

    Idempotent: a second run classifies nothing new and finds each unmapped
    line's item still open.
    """
    if now.tzinfo is None:
        raise ValidationFailedError(field="now", reason="time must be timezone-aware")
    report = denials.classify_denials(session, client_id=client_id)
    for line in report.unmapped:
        source_key = _source_key(line)
        if not _open(session, client_id=client_id, source_key=source_key):
            # Declining to classify is a decision like classifying (PD-1).
            # Recorded when the item is raised, not on every run that finds
            # it still open.
            record_decision(
                session,
                entity_type="remittance_service_line",
                entity_id=line.service_line_id,
                decision_type=DecisionType.CLASSIFY_DENIAL,
                decided_by=denials.DECIDED_BY,
                logic_version=denials.TAXONOMY_VERSION,
                input_snapshot={"refusal_carcs": list(line.carcs)},
                output={"held": "unmapped-refusal", "category": None},
            )
        exceptions.raise_exception(
            session,
            client_id=client_id,
            kind=WorkItemKind.DATA_QUALITY,
            # The codes first, so a cut title still names them.
            problem=(
                f"Refusal code(s) {', '.join(line.carcs)} on a denied 835 line are not in "
                f"denial taxonomy {denials.TAXONOMY_VERSION}; a person sets the category"
            ),
            record=exceptions.RecordRef("remittance_service_line", line.service_line_id),
            source_key=source_key,
            now=now,
        )
    session.flush()
    log.info("denials.routed", unmapped=len(report.unmapped))
    return report
