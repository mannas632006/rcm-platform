"""Domain entities.

Importing a model module registers its tables on `rcm.platform.db.Base`, which
is what Alembic compares the database against. They are re-exported here so
that `import rcm.domain` is enough for metadata to be complete -- a model that
is only registered when some other module happens to import it produces
autogenerate diffs that depend on import order.
"""

from __future__ import annotations

from rcm.domain.claim import MAX_MODIFIERS, Claim, ClaimLine
from rcm.domain.payer import MatchCandidateStatus, Payer, PayerAlias, PayerMatchCandidate
from rcm.domain.remittance import (
    VALID_CAS_GROUP_CODES,
    ClaimPayment,
    ProviderAdjustment,
    RawFile,
    RawFileStatus,
    Remittance,
    RemittanceAdjustment,
    RemittanceServiceLine,
)

__all__ = [
    "MAX_MODIFIERS",
    "VALID_CAS_GROUP_CODES",
    "Claim",
    "ClaimLine",
    "ClaimPayment",
    "MatchCandidateStatus",
    "Payer",
    "PayerAlias",
    "PayerMatchCandidate",
    "ProviderAdjustment",
    "RawFile",
    "RawFileStatus",
    "Remittance",
    "RemittanceAdjustment",
    "RemittanceServiceLine",
]
