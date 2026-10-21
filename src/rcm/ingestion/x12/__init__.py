"""X12 EDI parsing (M1).

Untrusted input, parsed in isolation (SR-12). `.importlinter` forbids this
package from importing the domain: the parser produces plain structures and a
loader elsewhere maps them onto entities, so a malformed file has no path into
CORE.

The layers are deliberately separate and separately testable:

    tokenizer   bytes  -> segments, with the delimiters the file declares
    envelope    segments -> ISA/GS/ST structure, with every control count checked
    remittance  one 835 transaction -> Remittance

`read_remittances` is the convenience path for the common case; anything that
needs to inspect a file that fails a later stage uses the three directly.
"""

from __future__ import annotations

from rcm.ingestion.x12.envelope import (
    FunctionalGroup,
    Interchange,
    TransactionSet,
    parse_interchange,
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
    "VALID_CAS_GROUPS",
    "Adjustment",
    "ClaimPayment",
    "FunctionalGroup",
    "Interchange",
    "Limits",
    "ProviderAdjustment",
    "Remittance",
    "Segment",
    "Separators",
    "ServiceLine",
    "TransactionSet",
    "decode",
    "detect_separators",
    "parse_835",
    "parse_interchange",
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
