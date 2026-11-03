"""Write an automatic correction to a stored claim (M4.7, ADR-0011).

The scrubber decides a correction; this module is the only place it lands
on the claim. Each change writes an `audit_event` in the caller's
transaction (PD-2) naming the field, the line and the fix, never the codes:
the before and after values are in the scrub's `decision_record`.

Nothing here can add a modifier the line did not carry. A correction that
would is refused (PD-16), whatever produced it.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.domain.claim import Claim, ClaimLine
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.errors import (
    ManualCodingRequiredError,
    ResourceNotFoundError,
    ValidationFailedError,
)

_MAX_MODIFIERS: Final = 4
#: PD-16. Repeated here, not imported: the domain sits below the rules engine.
_PROTECTED: Final = frozenset({"25", "59", "XE", "XP", "XS", "XU"})


def _audit(
    session: Session,
    *,
    claim_id: uuid.UUID,
    actor: str,
    field: str,
    fix: str,
    line_number: int | None,
) -> None:
    record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor,
        action="claim.auto_corrected",
        entity_type="claim",
        entity_id=claim_id,
        after_state={"field": field, "line_number": line_number, "fix": fix},
    )


def correct_diagnoses(
    session: Session,
    *,
    claim_id: uuid.UUID,
    client_id: uuid.UUID,
    diagnoses: list[str],
    fix: str,
    actor: str,
) -> None:
    claim = session.scalar(
        select(Claim)
        .where(Claim.claim_id == claim_id, Claim.client_id == client_id)
        .with_for_update()
    )
    if claim is None:
        raise ResourceNotFoundError(resource="claim")
    if len(diagnoses) != len(claim.diagnoses):
        # Line pointers index this list: a correction may rewrite a code,
        # never add, drop or reorder one.
        raise ValidationFailedError(field="diagnoses", reason="the same number of codes")
    claim.diagnoses = list(diagnoses)
    _audit(
        session, claim_id=claim_id, actor=actor, field="claim.diagnoses", fix=fix, line_number=None
    )


def correct_line_modifiers(
    session: Session,
    *,
    claim_id: uuid.UUID,
    client_id: uuid.UUID,
    line_number: int,
    modifiers: list[str],
    fix: str,
    actor: str,
) -> None:
    line = session.scalar(
        select(ClaimLine)
        .where(
            ClaimLine.claim_id == claim_id,
            ClaimLine.client_id == client_id,
            ClaimLine.line_number == line_number,
        )
        .with_for_update()
    )
    if line is None:
        raise ResourceNotFoundError(resource="claim_line")
    if len(modifiers) > _MAX_MODIFIERS:
        raise ValidationFailedError(field="modifiers", reason="at most four")
    current = [m for m in (line.modifier_1, line.modifier_2, line.modifier_3, line.modifier_4) if m]
    added = {m.upper() for m in modifiers} - {m.upper() for m in current}
    if added & _PROTECTED:
        raise ManualCodingRequiredError(field="modifiers", line_number=line_number)
    padded = [*modifiers, None, None, None, None][:_MAX_MODIFIERS]
    line.modifier_1, line.modifier_2, line.modifier_3, line.modifier_4 = padded
    _audit(
        session,
        claim_id=claim_id,
        actor=actor,
        field="line.modifiers",
        fix=fix,
        line_number=line_number,
    )
