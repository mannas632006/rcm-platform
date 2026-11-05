"""The payer registry's staleness and lookup rules (M4.10, BR-REG-01..03).

BR-REG-02: "Entries older than [TBD-42] days MUST be flagged stale and MUST
NOT silently drive a blocking decision." A stale entry still speaks -- as
advice that says it is stale -- but it cannot block.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rcm.decision.registry import EntryView, RegistryCheck, is_stale, select_entry
from rcm.decision.rules.definition import Layer
from rcm.decision.rules.evaluate import ClaimFacts, LineFacts, Mode, Outcome, Verdict, evaluate

TODAY = date(2026, 11, 3)
LIMIT = 180


def entry(
    *,
    cpt: str = "70553",
    plan_type: str = "*",
    auth: bool | None = True,
    verified: date = TODAY,
    version: int = 1,
) -> EntryView:
    return EntryView(
        payer_id="00590",
        plan_type=plan_type,
        cpt=cpt,
        version=version,
        prior_auth_required=auth,
        last_verified_date=verified,
        verified_by="registry.owner",
    )


def facts(*, cpts: tuple[str, ...] = ("70553",), has_auth: bool = False) -> ClaimFacts:
    return ClaimFacts(
        claim={
            "claim.payer_id": "00590",
            "claim.claim_frequency_code": "1",
            "claim.claim_filing_code": "CI",
            "claim.place_of_service": "11",
            "claim.total_charge": None,
            "claim.date_of_service": TODAY,
            "claim.has_prior_auth": has_auth,
            "claim.diagnoses": ["G43.909"],
            "claim.procedure_codes": list(cpts),
            "claim.line_count": len(cpts),
        },
        lines=tuple(
            LineFacts(
                number=n,
                values={
                    "line.procedure_code": cpt,
                    "line.modifiers": [],
                    "line.units": None,
                    "line.charge_amount": None,
                    "line.service_date": TODAY,
                },
            )
            for n, cpt in enumerate(cpts, start=1)
        ),
    )


def check(*entries: EntryView) -> RegistryCheck:
    return RegistryCheck(entries=entries, today=TODAY, staleness_days=LIMIT)


@pytest.mark.parametrize(
    ("age", "stale"), [(0, False), (LIMIT, False), (LIMIT + 1, True), (900, True)]
)
def test_an_entry_is_stale_only_past_the_threshold(age: int, stale: bool) -> None:
    assert is_stale(TODAY - timedelta(days=age), today=TODAY, staleness_days=LIMIT) is stale


def test_the_most_specific_entry_wins() -> None:
    """A procedure's own entry over the payer-wide one; a plan's own over all plans."""
    wide = entry(cpt="*", plan_type="*", auth=False)
    for_cpt = entry(cpt="70553", plan_type="*", auth=True)
    for_plan = entry(cpt="70553", plan_type="Commercial", auth=False)
    pool = (wide, for_cpt, for_plan)
    assert select_entry(pool, plan_type="Commercial", cpt="70553") is for_plan
    assert select_entry(pool, plan_type="Medicare", cpt="70553") is for_cpt
    assert select_entry(pool, plan_type="Medicare", cpt="99213") is wide
    assert select_entry((for_cpt,), plan_type="Medicare", cpt="99213") is None


def test_a_fresh_entry_blocks_a_claim_without_its_authorisation() -> None:
    result = evaluate([], facts(), checks=[check(entry())])
    (finding,) = result.findings
    assert (finding.layer, finding.mode, finding.outcome) == (
        Layer.PAYER_SPECIFIC,
        Mode.BLOCKING,
        Outcome.FLAGGED,
    )
    assert finding.route_to == "AUTH_PROBLEM"
    assert result.verdict is Verdict.BLOCK


@pytest.mark.invariant
def test_a_stale_entry_advises_but_never_blocks() -> None:
    """BR-REG-02 / UR-18: excluded from BLOCKING until re-verified, and says
    why -- never silently."""
    old = entry(verified=TODAY - timedelta(days=LIMIT + 1))
    result = evaluate([], facts(), checks=[check(old)])
    (finding,) = result.findings
    assert finding.mode is Mode.ADVISORY
    assert "stale" in finding.reference
    assert result.verdict is Verdict.ADVISE


def test_an_authorisation_on_the_claim_satisfies_the_entry() -> None:
    assert evaluate([], facts(has_auth=True), checks=[check(entry())]).findings == ()


@pytest.mark.parametrize("auth", [False, None])
def test_an_entry_that_does_not_require_authorisation_flags_nothing(auth: bool | None) -> None:
    """None is "not recorded", never "required": the registry asserts only
    what someone verified."""
    assert evaluate([], facts(), checks=[check(entry(auth=auth))]).findings == ()


def test_the_finding_names_the_entry_version_it_rests_on() -> None:
    """BR-REG-03: a claim records which registry version applied."""
    (finding,) = evaluate([], facts(), checks=[check(entry(version=4))]).findings
    assert finding.rule_version == 4
    assert "v4" in finding.reference
