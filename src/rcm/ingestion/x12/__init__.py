"""X12 EDI parsing (M1).

Untrusted input, parsed in isolation (SR-12). `.importlinter` forbids this
package from importing the domain: the parser produces plain structures and a
loader elsewhere maps them onto entities, so a malformed file has no path into
CORE.

The layers are deliberately separate and separately testable:

    tokenizer   bytes  -> segments, with the delimiters the file declares
    envelope    segments -> ISA/GS/ST structure, with every control count checked
    remittance  one 835 transaction -> Remittance
    claim       one 837 transaction -> ClaimSubmission, and back again
    acknowledgement  one 999 -> did the transmission parse
    claim_status     one 277/277CA -> did the payer accept the claim
    eligibility      one 271 -> is the patient covered
    fields      the element parsers the transaction parsers share

`read_remittances` and `read_claims` are the convenience paths for the common
case; anything that needs to inspect a file that fails a later stage uses the
layers directly.
"""

from __future__ import annotations

from rcm.ingestion.x12.acknowledgement import (
    FunctionalAcknowledgement,
    SegmentError,
    TransactionAcknowledgement,
    parse_999,
)
from rcm.ingestion.x12.claim import (
    STANDARD_SEPARATORS,
    BillingProvider,
    Claim837,
    ClaimServiceLine,
    ClaimSubmission,
    Subscriber,
    TradingPartner,
    build_837,
    parse_837,
)
from rcm.ingestion.x12.claim_status import (
    ClaimStatus,
    ClaimStatusResponse,
    parse_277,
)
from rcm.ingestion.x12.eligibility import (
    Benefit,
    BenefitPeriod,
    EligibilityResponse,
    RequestProblem,
    parse_271,
)
from rcm.ingestion.x12.envelope import (
    FunctionalGroup,
    Interchange,
    TransactionSet,
    parse_interchange,
)
from rcm.ingestion.x12.fields import (
    optional_amount,
    optional_date,
    parse_amount,
    parse_date,
    parse_quantity,
)
from rcm.ingestion.x12.remittance import (
    BALANCE_TOLERANCE,
    VALID_CAS_GROUPS,
    Adjustment,
    ClaimPayment,
    ProviderAdjustment,
    Remittance,
    ServiceLine,
    parse_835,
)
from rcm.ingestion.x12.tokenizer import (
    Limits,
    Segment,
    Separators,
    decode,
    detect_separators,
    tokenize,
)

__all__ = [
    "BALANCE_TOLERANCE",
    "STANDARD_SEPARATORS",
    "VALID_CAS_GROUPS",
    "Adjustment",
    "Benefit",
    "BenefitPeriod",
    "BillingProvider",
    "Claim837",
    "ClaimPayment",
    "ClaimServiceLine",
    "ClaimStatus",
    "ClaimStatusResponse",
    "ClaimSubmission",
    "EligibilityResponse",
    "FunctionalAcknowledgement",
    "FunctionalGroup",
    "Interchange",
    "Limits",
    "ProviderAdjustment",
    "Remittance",
    "RequestProblem",
    "Segment",
    "SegmentError",
    "Separators",
    "ServiceLine",
    "Subscriber",
    "TradingPartner",
    "TransactionAcknowledgement",
    "TransactionSet",
    "build_837",
    "decode",
    "detect_separators",
    "optional_amount",
    "optional_date",
    "parse_271",
    "parse_277",
    "parse_835",
    "parse_837",
    "parse_999",
    "parse_amount",
    "parse_date",
    "parse_interchange",
    "parse_quantity",
    "read_acknowledgements",
    "read_claim_statuses",
    "read_claims",
    "read_eligibility",
    "read_interchange",
    "read_remittances",
    "tokenize",
]


def read_interchange(source: str | bytes, *, limits: Limits | None = None) -> Interchange:
    """Tokenize and validate one interchange."""
    segments, separators = tokenize(source, limits=limits)
    return parse_interchange(segments, separators)


def read_remittances(
    source: str | bytes, *, limits: Limits | None = None
) -> tuple[Remittance, ...]:
    """Every 835 in one interchange.

    A file normally carries one, but the standard permits several and some
    clearinghouses batch them. Returning a tuple rather than a single
    `Remittance` means the caller cannot silently process the first and
    discard the rest -- which would lose real money with no error anywhere.
    """
    interchange = read_interchange(source, limits=limits)
    return tuple(
        parse_835(transaction, interchange.separators)
        for transaction in interchange.transaction_sets("835")
    )


def read_claims(
    source: str | bytes, *, limits: Limits | None = None
) -> tuple[ClaimSubmission, ...]:
    """Every 837 in one interchange.

    A tuple for the same reason `read_remittances` returns one: the standard
    permits several transaction sets per file, and a caller that silently
    processed the first would drop claims with no error anywhere.
    """
    interchange = read_interchange(source, limits=limits)
    return tuple(
        parse_837(transaction, interchange.separators)
        for transaction in interchange.transaction_sets("837")
    )


def read_acknowledgements(
    source: str | bytes, *, limits: Limits | None = None
) -> tuple[FunctionalAcknowledgement, ...]:
    """Every 999 in one interchange."""
    interchange = read_interchange(source, limits=limits)
    return tuple(
        parse_999(transaction, interchange.separators)
        for transaction in interchange.transaction_sets("999")
    )


def read_claim_statuses(
    source: str | bytes, *, limits: Limits | None = None
) -> tuple[ClaimStatusResponse, ...]:
    """Every 277 or 277CA in one interchange.

    Both carry ST01 "277": a 277CA is distinguished by its implementation
    guide (005010X214) rather than by a different transaction identifier, so
    filtering on ST01 returns both and the caller decides what the statuses
    mean.
    """
    interchange = read_interchange(source, limits=limits)
    return tuple(
        parse_277(transaction, interchange.separators)
        for transaction in interchange.transaction_sets("277")
    )


def read_eligibility(
    source: str | bytes, *, limits: Limits | None = None
) -> tuple[EligibilityResponse, ...]:
    """Every 271 in one interchange."""
    interchange = read_interchange(source, limits=limits)
    return tuple(
        parse_271(transaction, interchange.separators)
        for transaction in interchange.transaction_sets("271")
    )
