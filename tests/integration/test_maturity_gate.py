"""DR-09: an outcome that has not settled does not train anything (M2.6).

    DR-09 (MUST): No row enters final-outcome training until
    `outcome_matured_at` has passed (recommended 180 days post-DOS), because
    reversals and recoupments change outcomes retroactively.

A claim paid in March and recouped in September was, in April, a paid claim.
Anything that read it in April read it correctly and still got the wrong
answer, because the answer had not finished happening.

**Why this file exists rather than relying on the corpus.** The synthetic
corpus is dated 2024 and every row in it is long since mature, so removing
the maturity filter from `mart_final_outcome` changes nothing and the gate
passes whether or not it works. The M2 mutation pass found exactly that --
the control came back "not proven" because the corpus could not express the
shape it guards. Same lesson as M1.5's missing multi-modifier lines and
M1.10's missing reversals: a corpus that cannot fail the test does not test
it.

So this builds the shape directly: two claims identical but for their date
of service, one on each side of the boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from rcm.analytics.training import DEFAULT_MATURITY_DAYS
from rcm.ingestion.claim_loader import load_claim_file
from rcm.ingestion.x12.claim import (
    BillingProvider,
    Claim837,
    ClaimServiceLine,
    ClaimSubmission,
    Subscriber,
    TradingPartner,
    build_837,
)
from rcm.platform.db import reset_engines, session_scope

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT = uuid.UUID("55555555-0000-4000-8000-000000000055")

TABLES = (
    "claim_acknowledgement, claim_line, claim, remittance_adjustment, "
    "remittance_service_line, claim_payment, provider_adjustment, remittance, "
    "raw_file, payer_alias, payer, payer_match_candidate"
)

PROVIDER = BillingProvider(
    name="Lakeview Clinic", npi="4952654158", taxonomy_code="207Q00000X", tax_id="549656931"
)
SUBSCRIBER = Subscriber(
    last_name="Castellanos", first_name="Andre", member_id="VAZ957419823", sex="F"
)

#: UTC, not local time. `mart_final_outcome` compares against Postgres'
#: `current_date`, so a naive "today" here and the server's idea of today can
#: differ by a day around midnight -- which would put a row on the wrong side
#: of the boundary and fail the suite for reasons that have nothing to do
#: with the gate.
#:
#: Both dates sit comfortably inside and outside the window rather than on
#: it, for the same reason: a test that is only correct on most days is a
#: test that fails on a Tuesday for no reason anyone can reproduce.
_TODAY = datetime.now(UTC).date()
IMMATURE_DOS = _TODAY - timedelta(days=DEFAULT_MATURITY_DAYS // 2)
MATURE_DOS = _TODAY - timedelta(days=DEFAULT_MATURITY_DAYS * 2)


def _claim(claim_id: str, service_date: date) -> Claim837:
    return Claim837(
        claim_id=claim_id,
        total_charge=Decimal("186.49"),
        billing_provider=PROVIDER,
        subscriber=SUBSCRIBER,
        payer_id="00590",
        payer_name="BCBS Texas",
        claim_filing_code="CI",
        place_of_service="11",
        claim_frequency_code="1",
        diagnoses=("J06.9",),
        service_date=service_date,
        lines=(
            ClaimServiceLine(
                line_number=1,
                procedure_code="99203",
                charge_amount=Decimal("186.49"),
                diagnosis_pointers=(1,),
                service_date=service_date,
            ),
        ),
    )


def _file(*claims: Claim837, control: str) -> bytes:
    return build_837(
        ClaimSubmission(
            control_number=control,
            submitter=TradingPartner(name="RCM SEED BILLING", etin="SUBMITTER01"),
            receiver=TradingPartner(name="SEED CLEARINGHOUSE", etin="CLEARINGHSE1"),
            claims=claims,
            created_on=_TODAY,
        )
    ).encode()


def run_dbt(args: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "run_dbt.py"), *args],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"dbt {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture(scope="module")
def warehouse(owner_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    archive = tmp_path_factory.mktemp("raw-maturity")
    for claim_id, dos, control in (
        ("MATURE01", MATURE_DOS, "000100001"),
        ("FRESH0001", IMMATURE_DOS, "000100002"),
    ):
        with session_scope() as session:
            load_claim_file(
                session,
                _file(_claim(claim_id, dos), control=control),
                client_id=CLIENT,
                original_filename=f"{claim_id}.edi",
                archive_root=archive,
                received_at=datetime.now(UTC),
            )
    run_dbt(["build", "--select", "staging", "core", "marts"])
    yield owner_engine
    wipe()


def _ids(engine: Engine, table: str) -> set[str]:
    with engine.begin() as conn:
        return {
            row[0] for row in conn.execute(text(f"SELECT submitter_claim_id FROM {table}")).all()
        }


def test_both_claims_reach_the_unfiltered_mart(warehouse: Engine) -> None:
    """The control. If the fresh claim never arrived, the exclusion below
    would prove nothing -- a row absent because it was never loaded looks
    exactly like a row the gate removed."""
    present = _ids(warehouse, "analytics_marts.mart_claim_line_outcome")
    assert {"MATURE01", "FRESH0001"} <= present


@pytest.mark.invariant
def test_an_immature_outcome_is_excluded_from_final_outcome(warehouse: Engine) -> None:
    """DR-09. The claim whose outcome can still change does not reach the
    table that final-outcome metrics and training read from."""
    final = _ids(warehouse, "analytics_marts.mart_final_outcome")

    assert "MATURE01" in final, "a claim well past its maturity window was excluded"
    assert "FRESH0001" not in final, (
        "a claim still inside its maturity window reached final-outcome training; "
        "reversals and recoupments can still change its answer (DR-09)"
    )


def test_the_maturity_date_is_derived_from_the_date_of_service(warehouse: Engine) -> None:
    """Not from adjudication: a recoupment is timed against the service, not
    against the cheque that paid for it."""
    with warehouse.begin() as conn:
        matured_at = conn.execute(
            text(
                "SELECT DISTINCT outcome_matured_at FROM "
                "analytics_marts.mart_claim_line_outcome "
                "WHERE submitter_claim_id = 'FRESH0001'"
            )
        ).scalar_one()
    assert matured_at == IMMATURE_DOS + timedelta(days=DEFAULT_MATURITY_DAYS)
