"""The leakage suite. Blocking (M2.4).

    PD-11: Features must be knowable at submission time. No leakage.

The headline test is `test_no_feature_changes_when_the_future_is_removed`,
and it is deliberately not an argument about SQL. The warehouse is built
twice -- once with everything, once as it looked on a cutoff date -- and
every FEATURE value for the rows present in both builds must be identical.

That is DR-07's question asked empirically:

    "Could this value have been known the second before we pressed SEND?"

If recomputing a column without any data that arrived after the claim was
sent produces a different answer, then the first answer was built from
information that did not exist yet. No reading of the SQL is required and no
reviewer has to spot it. It does not matter how the column was written, what
it is named, or how many models it was laundered through.

Labels are expected to change between the two builds -- that is what makes
them labels -- so they are excluded, and the fact that they *do* change is
itself asserted, because a comparison where nothing moves proves nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from rcm.analytics.features import (
    FEATURE_COLUMNS,
    KEY_COLUMNS,
    LABEL_COLUMNS,
    MART_COLUMNS,
    PENDING_COLUMNS,
)
from rcm.ingestion.acknowledgement_loader import load_acknowledgement_file
from rcm.ingestion.claim_loader import load_claim_file
from rcm.ingestion.remittance_loader import load_remittance_file
from rcm.platform.db import reset_engines, session_scope

pytestmark = pytest.mark.leakage

REPO_ROOT = Path(__file__).resolve().parents[2]
MART = "analytics_marts.mart_claim_line_outcome"

#: The cutoff the truncated build uses. It sits in the middle of the corpus
#: so that both sides are non-trivial: rows before it have history behind
#: them and outcomes after it.
AS_OF = date(2024, 6, 30)

#: Where the truncated build lands. A separate schema prefix, so the
#: comparison is against a real second warehouse rather than the same tables
#: rebuilt in place -- which could not be compared at all.
TRUNCATED_PREFIX = "asof"


def run_dbt(args: list[str], *, extra_env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "run_dbt.py"), *args],
        cwd=REPO_ROOT,
        env={**os.environ, **(extra_env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"dbt {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


CORPUS = REPO_ROOT / "data" / "synthetic"
CLIENT = uuid.UUID("77777777-0000-4000-8000-000000000077")

TABLES = (
    "claim_acknowledgement, claim_line, claim, remittance_adjustment, "
    "remittance_service_line, claim_payment, provider_adjustment, remittance, "
    "raw_file, payer_alias, payer, payer_match_candidate"
)


def _load_corpus(archive_root: Path) -> None:
    for folder, loader in (
        ("837", load_claim_file),
        ("999", load_acknowledgement_file),
        ("277CA", load_acknowledgement_file),
        ("835", load_remittance_file),
    ):
        for path in sorted((CORPUS / "x12" / folder).glob("*.txt")):
            with session_scope() as session:
                loader(
                    session,
                    path.read_bytes(),
                    client_id=CLIENT,
                    original_filename=path.name,
                    archive_root=archive_root,
                )


@pytest.fixture(scope="module")
def warehouse(owner_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    """Load the corpus and build the warehouse once for the module.

    The suite loads its own data rather than relying on whatever happens to
    be in CORE. The integration suite truncates on teardown, so a leakage
    run that assumed a loaded database would quietly measure an empty mart
    and pass -- which is the exact failure mode this suite exists to prevent
    in the data it checks.
    """
    if not CORPUS.exists():
        pytest.skip("no synthetic corpus on disk; run `make seed`")

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    _load_corpus(tmp_path_factory.mktemp("raw-leakage"))
    run_dbt(["build", "--select", "staging", "core", "marts"])
    yield owner_engine
    wipe()


@pytest.fixture(scope="module")
def truncated(warehouse: Engine) -> Iterator[str]:
    """Build a second warehouse as it looked on `AS_OF`.

    `DBT_SCHEMA` moves the whole build to a parallel set of schemas, so the
    full build is still there to compare against.
    """
    run_dbt(
        [
            "build",
            "--select",
            "staging",
            "core",
            "marts",
            "--vars",
            f"{{as_of_date: '{AS_OF.isoformat()}'}}",
        ],
        extra_env={"DBT_SCHEMA": TRUNCATED_PREFIX},
    )
    yield f"{TRUNCATED_PREFIX}_marts.mart_claim_line_outcome"

    with warehouse.begin() as conn:
        for suffix in ("marts", "core", "staging"):
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TRUNCATED_PREFIX}_{suffix} CASCADE"))


def mart_columns(engine: Engine) -> dict[str, str]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'analytics_marts' "
                "AND table_name = 'mart_claim_line_outcome'"
            )
        ).all()
    return dict(rows)


# ---------------------------------------------------------------------------
# The registry and the mart agree
# ---------------------------------------------------------------------------


def test_every_mart_column_is_classified(warehouse: Engine) -> None:
    """A column nobody has classified fails the suite.

    This is the cheap half of the defence and it catches the common case: a
    column gets added, it is useful, and nobody stopped to ask whether it was
    an answer. You cannot add one here without saying which it is, and saying
    so is the moment the question gets asked.
    """
    actual = set(mart_columns(warehouse))
    registered = set(MART_COLUMNS)

    assert actual - registered == set(), f"unclassified columns: {sorted(actual - registered)}"
    assert registered - actual == set(), f"classified but absent: {sorted(registered - actual)}"


def test_every_srs_column_is_present_and_typed(warehouse: Engine) -> None:
    """SRS 5.5's acceptance condition: every column present and typed.

    Including the ones with no source yet. They are a contract -- something
    downstream is written against this shape -- and a column that appears
    later is a schema change nobody planned for.
    """
    columns = mart_columns(warehouse)
    for name, reason in PENDING_COLUMNS.items():
        assert name in columns, f"{name} is missing ({reason})"
        assert columns[name] != "", f"{name} has no type ({reason})"


def test_the_pending_columns_are_all_null(warehouse: Engine) -> None:
    """A placeholder that quietly acquired data would be a feature nobody
    reviewed. If one of these starts returning values, it is because a
    milestone filled it, and that milestone should be removing it from
    `PENDING_COLUMNS` in the same change."""
    with warehouse.begin() as conn:
        for name in PENDING_COLUMNS:
            populated = conn.execute(text(f"SELECT count({name}) FROM {MART}")).scalar_one()
            assert populated == 0, f"{name} is listed as pending but has {populated} values"


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def _rows(engine: Engine, table: str, columns: list[str]) -> dict[tuple[Any, ...], tuple[Any, ...]]:
    key = "claim_line_id, coalesce(adjustment_id::text, '')"
    selected = ", ".join(columns)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                f"SELECT claim_line_id, coalesce(adjustment_id::text, ''), {selected} "
                f"FROM {table} ORDER BY {key}"
            )
        ).all()
    return {(row[0], row[1]): tuple(row[2:]) for row in result}


@pytest.mark.invariant
def test_features_knowable_at_submission_time(warehouse: Engine, truncated: str) -> None:
    """PD-11 / DR-07, measured rather than argued.

    Build the warehouse as it looked on the cutoff date, and compare every
    FEATURE column against the full build for the rows that exist in both. A
    feature that changes when the future is removed was reading the future.
    """
    features = sorted(FEATURE_COLUMNS)
    full = _rows(warehouse, MART, features)
    past = _rows(warehouse, truncated, features)

    shared = set(full) & set(past)
    assert shared, "the truncated build shares no rows with the full one; the cutoff is wrong"

    offenders: dict[str, int] = {}
    for key in shared:
        for name, whole, limited in zip(features, full[key], past[key], strict=True):
            if whole != limited:
                offenders[name] = offenders.get(name, 0) + 1

    assert not offenders, (
        "these FEATURE columns changed when data from after submission was "
        f"removed, so they could not have been known at submission: {offenders}"
    )


def test_the_comparison_would_notice_a_change(warehouse: Engine, truncated: str) -> None:
    """The control for the test above.

    If the two builds were identical in every column, the leakage test would
    pass by comparing nothing. Labels *must* differ -- rows adjudicated after
    the cutoff have no outcome in the truncated build -- and asserting that
    they do is what proves the comparison has teeth.
    """
    labels = sorted(LABEL_COLUMNS)
    full = _rows(warehouse, MART, labels)
    past = _rows(warehouse, truncated, labels)

    shared = set(full) & set(past)
    changed = sum(1 for key in shared if full[key] != past[key])

    assert changed > 0, (
        "no label changed between the full and truncated builds, so the "
        "comparison is not actually comparing anything"
    )


def test_keys_are_stable_across_the_cutoff(warehouse: Engine, truncated: str) -> None:
    """A row that exists in both builds is the same row.

    If the keys moved, the feature comparison would be lining up rows that
    have nothing to do with each other and its silence would mean nothing.
    """
    keys = sorted(KEY_COLUMNS - {"adjustment_id", "service_line_id"})
    full = _rows(warehouse, MART, keys)
    past = _rows(warehouse, truncated, keys)

    for key in set(full) & set(past):
        assert full[key] == past[key], f"key columns differ for {key}"


def test_no_claim_is_adjudicated_before_it_was_submitted(warehouse: Engine) -> None:
    """Data integrity under the whole thing.

    A negative turnaround is impossible, so if one appears the claim-to-
    remittance join has matched the wrong rows -- and every trailing feature
    built on that join is then measuring something else. Same-day (zero) is
    legitimate and common for clean electronic claims.
    """
    with warehouse.begin() as conn:
        offenders = conn.execute(
            text(f"SELECT count(*) FROM {MART} WHERE days_submission_to_adjudication < 0")
        ).scalar_one()
    assert offenders == 0, f"{offenders} rows were adjudicated before they were submitted"


@pytest.mark.invariant
def test_the_trailing_rate_matches_an_independent_recomputation(warehouse: Engine) -> None:
    """DR-07's window bound, checked against a second implementation.

    The model computes the window with an ordered window function, because a
    correlated subquery per row does not finish on a real corpus. That makes
    the bound -- `between 90 days preceding and 1 day preceding` -- a single
    expression, and a single character of it is the difference between
    looking backwards and looking forwards.

    So it is checked against the slow, obvious version: for each line, count
    the adjudications that strictly precede its submission date. It is
    O(n^2) and that is fine for a test; what matters is that it shares no
    code with the thing it checks, and that it states the rule in the form a
    reader can verify by eye.
    """
    with warehouse.begin() as conn:
        disagreements = conn.execute(
            text("""
                with events as (
                    select
                        sub.payer_id,
                        ls.procedure_code,
                        la.payment_date as known_on,
                        case when la.line_paid = 0 and exists (
                            select 1 from analytics_core.core_line_adjustment a
                            where a.service_line_id = la.service_line_id
                        ) then 1 else 0 end as denied
                    from analytics_core.core_line_adjudication la
                    join analytics_core.core_claim_adjudication ca
                        on la.claim_payment_id = ca.claim_payment_id
                    join analytics_core.core_claim_submission sub
                        on ca.submitter_claim_id = sub.submitter_claim_id
                       and ca.client_id = sub.client_id
                    join analytics_core.core_line_submission ls
                        on ls.claim_id = sub.claim_id
                       and ls.procedure_code = la.procedure_code
                       and ls.procedure_occurrence = la.procedure_occurrence
                    where la.payment_date is not null
                ),
                recomputed as (
                    select
                        m.claim_line_id,
                        m.payer_cpt_window_size as model_size,
                        m.payer_cpt_denial_rate_90d as model_rate,
                        (
                            select count(*) from events e
                            where e.payer_id = m.payer_id
                              and e.procedure_code = m.cpt_hcpcs
                              and e.known_on < m.submission_date
                              and e.known_on >= m.submission_date - 90
                        ) as slow_size,
                        (
                            select sum(e.denied) from events e
                            where e.payer_id = m.payer_id
                              and e.procedure_code = m.cpt_hcpcs
                              and e.known_on < m.submission_date
                              and e.known_on >= m.submission_date - 90
                        ) as slow_denied
                    from (select distinct claim_line_id, payer_id, cpt_hcpcs,
                                 submission_date, payer_cpt_window_size,
                                 payer_cpt_denial_rate_90d
                          from analytics_marts.mart_claim_line_outcome) m
                )
                select count(*) from recomputed
                where model_size is distinct from slow_size
                   or model_rate is distinct from case
                        when slow_size > 0 then round(slow_denied::numeric / slow_size, 6)
                      end
            """)
        ).scalar_one()

    assert disagreements == 0, (
        f"{disagreements} lines where the windowed trailing rate disagrees with a "
        "strictly-backwards recomputation; the window bound is wrong"
    )
