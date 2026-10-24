"""The parser against the whole synthetic corpus, checked by a parser that
shares none of its code (M1 gate).

A parser agreeing with itself is not evidence. `tools/synthetic/validate.py`
re-reads the emitted text from scratch with an independent implementation, and
was written before this one; where the two disagree, at least one is wrong and
the disagreement is the finding.

This is the test that would catch the defects that unit fixtures cannot,
because a fixture only contains what its author already thought of. The corpus
contains several hundred files with multi-CAS lines, reversals, recoupments,
resubmissions, secondary coverage, zero-payment remittances and one
deliberately unbalanced file.
"""

from __future__ import annotations

import collections
import csv
import json
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from rcm.ingestion.x12 import read_remittances
from rcm.platform.errors import PlatformError

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_CONFIG = REPO_ROOT / "tools" / "synthetic" / "config" / "smoke.yaml"


@pytest.fixture(scope="module")
def corpus() -> Path:
    """Generate the smoke corpus once for this module."""
    from tools.synthetic.generate import OUT

    if OUT.exists():
        shutil.rmtree(OUT)
    result = subprocess.run(
        [sys.executable, "-m", "tools.synthetic", "--config", str(SMOKE_CONFIG)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"generator failed:\n{result.stdout}\n{result.stderr}"
    return OUT


def _remittance_files(corpus: Path) -> list[Path]:
    files = sorted((corpus / "x12" / "835").glob("*.txt"))
    assert files, "no 835 files in the corpus"
    return files


def test_every_remittance_in_the_corpus_parses(corpus: Path) -> None:
    """M1 gate: ingest the full corpus. Nothing raises, nothing is skipped."""
    failures: list[tuple[str, str, object]] = []
    parsed = 0

    for path in _remittance_files(corpus):
        try:
            remittances = read_remittances(path.read_bytes())
        except PlatformError as exc:
            failures.append((path.name, exc.code, exc.context))
            continue
        assert remittances, f"{path.name} produced no remittance"
        parsed += len(remittances)

    assert not failures, f"{len(failures)} file(s) failed to parse: {failures[:3]}"
    assert parsed >= 200


def test_the_parser_agrees_with_the_independent_reference(corpus: Path) -> None:
    """Claim, line, adjustment and money totals, file by file.

    Counts are compared per file rather than in aggregate: two offsetting
    errors would cancel in a total and show up here.
    """
    from tools.synthetic.validate import parse_835 as reference_835

    mismatches: list[str] = []

    for path in _remittance_files(corpus):
        reference = reference_835(path.read_text(encoding="utf-8"))
        mine = read_remittances(path.read_bytes())[0]

        checks: list[tuple[str, object, object]] = [
            ("claims", len(reference["claims"]), len(mine.claims)),
            (
                "lines",
                sum(len(c["lines"]) for c in reference["claims"]),
                sum(len(c.service_lines) for c in mine.claims),
            ),
            ("adjustments", len(reference["adjustments"]), len(mine.adjustments)),
            ("payment_total", reference["payment_total"], mine.payment_amount),
            ("plb_total", reference["plb_total"], mine.provider_adjustment_total),
            ("balances", reference["balances"], mine.balances),
        ]
        for name, expected, actual in checks:
            if expected != actual:
                mismatches.append(f"{path.name}: {name} reference={expected!r} parser={actual!r}")

    assert not mismatches, "\n".join(mismatches[:10])


@pytest.mark.invariant
def test_corpus_multi_cas_lines_are_preserved(corpus: Path) -> None:
    """PD-6 against real volume rather than one fixture.

    A parser that keeps only the first reason of each CAS segment passes every
    single-reason test in the suite and fails here.
    """
    lines_with_several_reasons = 0
    total_adjustments = 0

    for path in _remittance_files(corpus):
        for remittance in read_remittances(path.read_bytes()):
            for claim in remittance.claims:
                for line in claim.service_lines:
                    total_adjustments += len(line.adjustments)
                    if len(line.adjustments) > 1:
                        lines_with_several_reasons += 1

    assert lines_with_several_reasons >= 100, (
        f"only {lines_with_several_reasons} multi-reason lines found; "
        "either the corpus changed or reasons are being collapsed"
    )
    assert total_adjustments > lines_with_several_reasons


@pytest.mark.invariant
def test_every_adjustment_in_the_corpus_carries_a_valid_group(corpus: Path) -> None:
    """PD-3 across the whole corpus."""
    from rcm.ingestion.x12 import VALID_CAS_GROUPS

    groups: collections.Counter[str] = collections.Counter()
    for path in _remittance_files(corpus):
        for remittance in read_remittances(path.read_bytes()):
            for adjustment in remittance.adjustments:
                assert adjustment.group, f"{path.name}: adjustment without a group code"
                assert adjustment.group in VALID_CAS_GROUPS
                groups[adjustment.group] += 1

    # Both halves of the CO/PR distinction must be present, or the corpus is
    # not exercising the one that matters most (PD-19).
    assert groups["CO"] > 0
    assert groups["PR"] > 0


def test_exactly_one_corpus_file_fails_the_balance_assertion(corpus: Path) -> None:
    """The deliberate quarantine fixture, and nothing else (M1.3's input).

    If this count rises, either the generator broke or the parser is
    mishandling PLB -- both of which are worth failing a build over.
    """
    unbalanced = [
        path.name
        for path in _remittance_files(corpus)
        for remittance in read_remittances(path.read_bytes())
        if not remittance.balances
    ]

    assert unbalanced == [name for name in unbalanced if "UNBALANCED" in name], (
        f"a file that was supposed to balance does not: {unbalanced}"
    )
    assert len(unbalanced) == 1


def test_every_service_line_in_the_corpus_balances(corpus: Path) -> None:
    """SVC02 == SVC03 + sum(CAS), per line, across the corpus.

    This is the check that catches a dropped adjustment: the claim totals can
    still look right while an individual line quietly does not add up.
    """
    offenders: list[str] = []

    for path in _remittance_files(corpus):
        for remittance in read_remittances(path.read_bytes()):
            if "UNBALANCED" in path.name:
                continue
            for claim_id, procedure in remittance.unbalanced_service_lines:
                offenders.append(f"{path.name}: {claim_id} line {procedure}")

    assert not offenders, offenders[:10]


def test_parsed_totals_reconcile_exactly_with_the_generator(corpus: Path) -> None:
    """Every service line and adjustment the generator emitted comes back out.

    Exact equality, not a tolerance. An off-by-one here means a line is being
    dropped or duplicated somewhere in the corpus, and a `>=` assertion would
    hide exactly that.

    Two kinds of file are excluded and counted separately.

    The quarantine fixture is written outside the normal claim flow -- it
    exists to be structurally wrong -- so its rows never reach the CSV the
    manifest counts. Folding it into the comparison was the first version of
    this test, and it failed with 1025 against 1018: seven lines and three
    claims that are real output of the parser and correctly absent from the
    generator's own tally.

    Reversal files are excluded for the opposite reason: their rows are a
    *second* remittance for claims the CSV has already counted once. A
    reversal re-states a claim in order to take the payment back, so counting
    its lines here would double-count every reversed claim -- which is the
    same arithmetic error BR-DATA-03 forbids for resubmissions, arriving from
    the remittance side instead.
    """
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    regular: collections.Counter[str] = collections.Counter()
    quarantine_fixture: collections.Counter[str] = collections.Counter()
    reversals: collections.Counter[str] = collections.Counter()

    for path in _remittance_files(corpus):
        if "REVERSAL" in path.name:
            bucket = reversals
        elif "UNBALANCED" in path.name:
            bucket = quarantine_fixture
        else:
            bucket = regular
        for remittance in read_remittances(path.read_bytes()):
            bucket["claims"] += len(remittance.claims)
            bucket["lines"] += sum(len(c.service_lines) for c in remittance.claims)
            bucket["adjustments"] += len(remittance.adjustments)
            bucket["payment"] += 1

    # PD-4: a claim rejected before adjudication receives no 835, so the
    # remittance totals are short by exactly those claims. The adjudicated
    # subset is read from the generator's own CSV rather than assumed, so a
    # change in the rejection rate cannot quietly become a change in what
    # this test tolerates.
    with (corpus / "csv" / "claim_lines.csv").open(encoding="utf-8") as handle:
        csv_lines = list(csv.DictReader(handle))
    adjudicated = [r for r in csv_lines if r.get("reached_adjudication", "Y") == "Y"]
    adjudicated_ids = {r["claim_id"] for r in adjudicated}
    with (corpus / "csv" / "adjustments.csv").open(encoding="utf-8") as handle:
        csv_adjustments = [r for r in csv.DictReader(handle) if r["claim_id"] in adjudicated_ids]

    assert len(adjudicated) < len(csv_lines), "no rejected claims - PD-4 untested here"
    assert regular["lines"] == len(adjudicated)
    assert regular["adjustments"] == len(csv_adjustments)
    assert manifest["counts"]["service_lines"] == len(csv_lines)
    assert regular["claims"] > 0

    # The fixture is one file carrying a handful of claims. If it grows into
    # something substantial, the exclusion above stops being negligible and
    # this test should be revisited rather than quietly widened.
    assert quarantine_fixture["claims"] <= 10

    # Every reversal is one claim in one file, and there is one per reversed
    # claim. If that stops being true the exclusion above is hiding something.
    assert reversals["claims"] == manifest["counts"]["reversed_claims"]
    assert reversals["payment"] == manifest["counts"]["reversed_claims"]

    total_paid = sum(
        (
            remittance.payment_amount
            for path in _remittance_files(corpus)
            for remittance in read_remittances(path.read_bytes())
        ),
        Decimal("0"),
    )
    assert total_paid > Decimal("0")
