"""The payer registry: dated payer facts, and the check that reads them
(M4.10, DOC-02 9, ADR-0014).

DOC-02: "Not a module, but the reference data that every module depends on.
It is hand-maintained and will outperform any model." And it decays: payers
change rules without notice, so an entry nobody has re-verified is a guess.

- BR-REG-01: every entry carries `last_verified_date` and `verified_by`.
- BR-REG-02: past [TBD-42] days an entry is stale, and must not silently
  drive a blocking decision. Here it still speaks -- as advice that says it
  is stale -- but it cannot block until a person re-verifies it.
- BR-REG-03: versioned, insert-only; each scrub records the versions it read.

The first fact the registry drives is prior authorisation per payer and
procedure, as a Layer 4 check beside the rules (SRS 8.2).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rcm.decision.registry_models import WILDCARD, PayerRegistryEntry
from rcm.decision.rules.definition import Layer
from rcm.decision.rules.evaluate import ClaimFacts, Finding, Mode, Outcome
from rcm.domain.payer import Payer
from rcm.platform.config import get_settings
from rcm.platform.errors import (
    ConcurrencyConflictError,
    ResourceNotFoundError,
    ValidationFailedError,
)

#: The finding's name, as NCCI's are NCCI_PTP and NCCI_MUE.
PRIOR_AUTH_CHECK: Final = "REGISTRY_PRIOR_AUTH"
_ROUTE: Final = "AUTH_PROBLEM"
_MESSAGE: Final = (
    "The payer registry says this payer requires prior authorisation for this "
    "procedure, and the claim carries none."
)


@dataclass(frozen=True, slots=True)
class EntryView:
    payer_id: str
    plan_type: str
    cpt: str
    version: int
    prior_auth_required: bool | None
    last_verified_date: date
    verified_by: str

    @classmethod
    def of(cls, row: PayerRegistryEntry) -> EntryView:
        return cls(
            payer_id=row.payer_id,
            plan_type=row.plan_type,
            cpt=row.cpt,
            version=row.version,
            prior_auth_required=row.prior_auth_required,
            last_verified_date=row.last_verified_date,
            verified_by=row.verified_by,
        )


def is_stale(verified_on: date, *, today: date, staleness_days: int) -> bool:
    """Older than the threshold. On the threshold day itself it still holds."""
    return (today - verified_on).days > staleness_days


def select_entry(
    entries: Iterable[EntryView], *, plan_type: str | None, cpt: str
) -> EntryView | None:
    """The most specific entry for a procedure: its own over the payer-wide
    one, and a plan type's own over every plan's."""
    by_key = {(e.cpt, e.plan_type): e for e in entries}
    plans = [plan_type, WILDCARD] if plan_type else [WILDCARD]
    for wanted_cpt in (cpt, WILDCARD):
        for wanted_plan in plans:
            found = by_key.get((wanted_cpt, wanted_plan))
            if found is not None:
                return found
    return None


@dataclass(frozen=True, slots=True)
class RegistryCheck:
    """The current entries of one claim's payer, as a Layer 4 check."""

    entries: Sequence[EntryView]
    today: date
    staleness_days: int
    plan_type: str | None = None
    layer: Layer = field(default=Layer.PAYER_SPECIFIC)

    def _applied(self, facts: ClaimFacts) -> list[tuple[int, EntryView]]:
        applied = []
        for line in facts.lines:
            entry = select_entry(
                self.entries, plan_type=self.plan_type, cpt=str(line.values["line.procedure_code"])
            )
            if entry is not None:
                applied.append((line.number, entry))
        return applied

    def findings(self, facts: ClaimFacts) -> list[Finding]:
        if facts.claim["claim.has_prior_auth"] is True:
            return []
        found = []
        for number, entry in self._applied(facts):
            if entry.prior_auth_required is not True:
                continue
            stale = is_stale(
                entry.last_verified_date, today=self.today, staleness_days=self.staleness_days
            )
            reference = (
                f"payer-registry {entry.payer_id}/{entry.plan_type}/{entry.cpt} "
                f"v{entry.version} verified {entry.last_verified_date.isoformat()}"
            )
            if stale:
                reference += (
                    f"; stale: older than {self.staleness_days} days, advisory until "
                    "re-verified (BR-REG-02)"
                )
            found.append(
                Finding(
                    rule_id=PRIOR_AUTH_CHECK,
                    rule_version=entry.version,
                    layer=Layer.PAYER_SPECIFIC,
                    mode=Mode.ADVISORY if stale else Mode.BLOCKING,
                    outcome=Outcome.FLAGGED,
                    line_number=number,
                    message=_MESSAGE,
                    route_to=_ROUTE,
                    reference=reference,
                )
            )
        return found

    def snapshot(self, facts: ClaimFacts) -> list[dict[str, object]]:
        """The entries this claim's lines read, for its decision record."""
        seen: dict[tuple[str, str, int], dict[str, object]] = {}
        for _, entry in self._applied(facts):
            seen[(entry.plan_type, entry.cpt, entry.version)] = {
                "payer_id": entry.payer_id,
                "plan_type": entry.plan_type,
                "cpt": entry.cpt,
                "version": entry.version,
                "last_verified_date": entry.last_verified_date.isoformat(),
                "stale": is_stale(
                    entry.last_verified_date,
                    today=self.today,
                    staleness_days=self.staleness_days,
                ),
            }
        return list(seen.values())


def _current(session: Session, payer_id: str) -> list[EntryView]:
    """The latest version of each of a payer's entries."""
    rows = session.scalars(
        select(PayerRegistryEntry)
        .where(PayerRegistryEntry.payer_id == payer_id)
        .order_by(
            PayerRegistryEntry.plan_type,
            PayerRegistryEntry.cpt,
            PayerRegistryEntry.version.desc(),
        )
    )
    latest: dict[tuple[str, str], EntryView] = {}
    for row in rows:
        latest.setdefault((row.plan_type, row.cpt), EntryView.of(row))
    return list(latest.values())


def check_for(session: Session, *, payer_id: str, today: date) -> RegistryCheck:
    """The registry check for a claim to this payer, as of `today`."""
    payer = session.get(Payer, payer_id)
    return RegistryCheck(
        entries=_current(session, payer_id),
        today=today,
        staleness_days=get_settings().payer_registry_staleness_days,
        plan_type=payer.plan_type if payer is not None else None,
    )


def history(
    session: Session, *, payer_id: str, plan_type: str = WILDCARD, cpt: str = WILDCARD
) -> list[EntryView]:
    """Every version of one entry, oldest first: S-07's change history."""
    return [
        EntryView.of(row)
        for row in session.scalars(
            select(PayerRegistryEntry)
            .where(
                PayerRegistryEntry.payer_id == payer_id,
                PayerRegistryEntry.plan_type == plan_type,
                PayerRegistryEntry.cpt == cpt,
            )
            .order_by(PayerRegistryEntry.version)
        )
    ]


def _require(value: str, name: str) -> None:
    if not value.strip():
        raise ValidationFailedError(field=name, reason="must be named")


def record_entry(
    session: Session,
    *,
    payer_id: str,
    plan_type: str = WILDCARD,
    cpt: str = WILDCARD,
    prior_auth_required: bool | None,
    source_url: str | None,
    verified_on: date,
    verified_by: str,
    recorded_by: str,
    reason: str,
    today: date,
) -> EntryView:
    """Store a new version of an entry. Nothing is overwritten (BR-REG-03)."""
    for value, name in (
        (plan_type, "plan_type"),
        (cpt, "cpt"),
        (verified_by, "verified_by"),
        (recorded_by, "recorded_by"),
        (reason, "reason"),
    ):
        _require(value, name)
    if verified_on > today:
        # BR-REG-01: a verification is a thing that happened.
        raise ValidationFailedError(field="verified_on", reason="not a date in the future")
    if session.get(Payer, payer_id) is None:
        raise ResourceNotFoundError(resource="payer")
    latest = session.scalar(
        select(func.max(PayerRegistryEntry.version)).where(
            PayerRegistryEntry.payer_id == payer_id,
            PayerRegistryEntry.plan_type == plan_type,
            PayerRegistryEntry.cpt == cpt,
        )
    )
    row = PayerRegistryEntry(
        payer_id=payer_id,
        plan_type=plan_type,
        cpt=cpt,
        version=(latest or 0) + 1,
        prior_auth_required=prior_auth_required,
        source_url=source_url,
        last_verified_date=verified_on,
        verified_by=verified_by,
        reason=reason,
        recorded_by=recorded_by,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        # Two people saved the same entry at once; the second retries on top.
        raise ConcurrencyConflictError(resource="payer_registry_entry") from None
    return EntryView.of(row)


def verify_entry(
    session: Session,
    *,
    payer_id: str,
    plan_type: str = WILDCARD,
    cpt: str = WILDCARD,
    verified_on: date,
    verified_by: str,
    today: date,
) -> EntryView:
    """S-07's verification action: the same facts, confirmed today, as a new
    version -- so the history shows who vouched for it, and when."""
    versions = history(session, payer_id=payer_id, plan_type=plan_type, cpt=cpt)
    if not versions:
        raise ResourceNotFoundError(resource="payer_registry_entry")
    latest = session.scalars(
        select(PayerRegistryEntry).where(
            PayerRegistryEntry.payer_id == payer_id,
            PayerRegistryEntry.plan_type == plan_type,
            PayerRegistryEntry.cpt == cpt,
            PayerRegistryEntry.version == versions[-1].version,
        )
    ).one()
    return record_entry(
        session,
        payer_id=payer_id,
        plan_type=plan_type,
        cpt=cpt,
        prior_auth_required=latest.prior_auth_required,
        source_url=latest.source_url,
        verified_on=verified_on,
        verified_by=verified_by,
        recorded_by=verified_by,
        reason="re-verified",
        today=today,
    )
