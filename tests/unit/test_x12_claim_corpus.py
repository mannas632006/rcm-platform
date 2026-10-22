"""The 837 parser against the whole synthetic corpus (M1.5).

The round-trip property proves the parser and generator agree with each other.
That is necessary and not sufficient: two halves of one module can share a
misreading of the format and round-trip it perfectly. This is the other half of
the evidence -- 325 files written by `tools/synthetic/x12.py`, which was
written first and shares no code with the parser, checked against
`claim_headers.csv` and `claim_lines.csv`, which that generator wrote from the
claim objects *before* serialising them.

So the chain compared here is:

    claim object -> X12 text -> parsed claim
            \\-> CSV row ------------^

Comparison is claim by claim and line by line rather than in totals. Two
offsetting errors cancel in a sum, and the first version of the 835 corpus test
learned that the expensive way.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from rcm.ingestion.x12 import read_claims
from rcm.ingestion.x12.claim import Claim837, build_837

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


@pytest.fixture(scope="module")
def parsed(corpus: Path) -> dict[str, object]:
    """Every 837 in the corpus, parsed and indexed by claim id."""
    files = sorted((corpus / "x12" / "837").glob("*.txt"))
    assert files, "no 837 files in the corpus"

    claims: dict[str, Claim837] = {}
    transactions = 0
    for path in files:
        for submission in read_claims(path.read_bytes()):
            transactions += 1
            for claim in submission.claims:
                assert claim.claim_id not in claims, f"duplicate claim {claim.claim_id}"
                claims[claim.claim_id] = claim

    return {"files": len(files), "transactions": transactions, "claims": claims}


@pytest.fixture(scope="module")
def expected(corpus: Path) -> dict[str, object]:
    """The generator's own CSVs, which it wrote from the claim objects."""
    with (corpus / "csv" / "claim_headers.csv").open(encoding="utf-8") as handle:
        headers = {row["claim_id"]: row for row in csv.DictReader(handle)}

    lines: dict[str, list[dict[str, str]]] = defaultdict(list)
    with (corpus / "csv" / "claim_lines.csv").open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            lines[row["claim_id"]].append(row)

    return {
        "headers": headers,
        "lines": lines,
        "manifest": json.loads((corpus / "manifest.json").read_text(encoding="utf-8")),
    }


def _claims(parsed: dict[str, object]) -> dict[str, Claim837]:
    claims = parsed["claims"]
    assert isinstance(claims, dict)
    return claims


def test_every_file_parses(parsed: dict[str, object]) -> None:
    """No file in the corpus raises, and none is silently empty."""
    assert parsed["files"] == parsed["transactions"]
    assert len(_claims(parsed)) > 0


def test_exactly_the_generated_claims_are_parsed(
    parsed: dict[str, object], expected: dict[str, object]
) -> None:
    """Set equality, not a count.

    A count matches when one claim is dropped and another double-counted,
    which is the failure a total cannot see.
    """
    headers = expected["headers"]
    assert isinstance(headers, dict)

    assert set(_claims(parsed)) == set(headers)
    manifest = expected["manifest"]
    assert isinstance(manifest, dict)
    assert len(_claims(parsed)) == manifest["counts"]["claims"]


def test_every_claim_header_matches_the_generator(
    parsed: dict[str, object], expected: dict[str, object]
) -> None:
    """Compared field by field, claim by claim."""
    headers = expected["headers"]
    assert isinstance(headers, dict)

    mismatches: list[str] = []
    for claim_id, claim in sorted(_claims(parsed).items()):
        row = headers[claim_id]
        checks = {
            "total_charge": (claim.total_charge, Decimal(row["total_claim_charge"])),
            "payer_id": (claim.payer_id, row["payer_id"]),
            "place_of_service": (claim.place_of_service, row["place_of_service"]),
            "billing_npi": (claim.billing_provider.npi, row["billing_npi"]),
            "service_date": (
                claim.service_date.isoformat() if claim.service_date else "",
                row["date_of_service"],
            ),
            "original_claim_id": (claim.original_claim_id, row["original_claim_id"]),
            "line_count": (len(claim.lines), int(row["line_count_on_claim"])),
        }
        for field, (got, want) in checks.items():
            if got != want:
                mismatches.append(f"{claim_id}.{field}: parsed {got!r} != generated {want!r}")

    assert not mismatches, "\n".join(mismatches[:20])


def test_every_service_line_matches_the_generator(
    parsed: dict[str, object], expected: dict[str, object]
) -> None:
    """Line by line, including the modifiers and the diagnosis pointers.

    The pointers are the part most likely to be quietly wrong: they are a
    composite of one-based indexes into a list whose order the parser also has
    to preserve, so an off-by-one here points every line at the wrong
    diagnosis while the file still looks perfectly sensible.
    """
    line_rows = expected["lines"]
    assert isinstance(line_rows, dict)

    mismatches: list[str] = []
    total_lines = 0
    for claim_id, claim in sorted(_claims(parsed).items()):
        rows = sorted(line_rows[claim_id], key=lambda r: int(r["line_number"]))
        if len(rows) != len(claim.lines):
            mismatches.append(f"{claim_id}: parsed {len(claim.lines)} lines, generated {len(rows)}")
            continue
        for line, row in zip(claim.lines, rows, strict=True):
            total_lines += 1
            modifiers = tuple(row[f"modifier_{n}"] for n in (1, 2, 3, 4) if row[f"modifier_{n}"])
            pointers = tuple(int(p) for p in row["dx_pointers"].split(",") if p.strip())
            checks = {
                "line_number": (line.line_number, int(row["line_number"])),
                "procedure": (line.procedure_code, row["cpt_hcpcs"]),
                "modifiers": (line.modifiers, modifiers),
                "charge": (line.charge_amount, Decimal(row["line_charge_amount"])),
                "units": (line.units, Decimal(row["units"])),
                "dx_pointers": (line.diagnosis_pointers, pointers),
            }
            for field, (got, want) in checks.items():
                if got != want:
                    mismatches.append(
                        f"{claim_id}.{line.line_number}.{field}: "
                        f"parsed {got!r} != generated {want!r}"
                    )

    assert not mismatches, "\n".join(mismatches[:20])
    manifest = expected["manifest"]
    assert isinstance(manifest, dict)
    assert total_lines == manifest["counts"]["service_lines"]


def test_the_principal_diagnosis_is_first(
    parsed: dict[str, object], expected: dict[str, object]
) -> None:
    """HI carries the principal diagnosis under ABK and the rest under ABF.

    Order is load-bearing because the service line pointers index into it.
    """
    line_rows = expected["lines"]
    assert isinstance(line_rows, dict)

    mismatches: list[str] = []
    for claim_id, claim in sorted(_claims(parsed).items()):
        row = line_rows[claim_id][0]
        generated = tuple(row[name] for name in ("primary_dx", "dx_2", "dx_3", "dx_4") if row[name])
        if claim.diagnoses != generated:
            mismatches.append(f"{claim_id}: parsed {claim.diagnoses} != generated {generated}")

    assert not mismatches, "\n".join(mismatches[:20])


@pytest.mark.invariant
def test_resubmission_chains_to_original_across_the_corpus(
    parsed: dict[str, object], expected: dict[str, object]
) -> None:
    """PD-7 at corpus volume.

    The generator emits a known number of resubmissions, and each names the
    claim it replaces. A parser that drops REF*F8 reports zero here and every
    denial rate computed later is wrong in the same direction.
    """
    headers = expected["headers"]
    manifest = expected["manifest"]
    assert isinstance(headers, dict)
    assert isinstance(manifest, dict)

    chained = {
        claim_id: claim.original_claim_id
        for claim_id, claim in _claims(parsed).items()
        if claim.is_resubmission
    }
    from_csv = {
        claim_id: row["original_claim_id"]
        for claim_id, row in headers.items()
        if row["original_claim_id"]
    }

    assert chained == from_csv
    assert len(chained) == manifest["counts"]["resubmissions"]
    # Every chain points at a claim that exists, or it is not a chain.
    assert set(chained.values()) <= set(headers)


def test_no_corpus_line_violates_the_diagnosis_pointer_rule(
    parsed: dict[str, object],
) -> None:
    """BR-M6-01 over the whole corpus.

    The parser reports rather than raises, so this is the test that would
    notice the generator starting to emit lines without pointers -- which is
    how the rule would stop being exercised without anyone noticing.
    """
    offenders = {
        claim_id: claim.lines_without_diagnosis_pointer
        for claim_id, claim in _claims(parsed).items()
        if claim.lines_without_diagnosis_pointer
    }
    assert offenders == {}


def test_every_claim_total_equals_its_line_charges(parsed: dict[str, object]) -> None:
    unreconciled = {
        claim_id: (claim.total_charge, claim.line_charge_total)
        for claim_id, claim in _claims(parsed).items()
        if not claim.charges_reconcile
    }
    assert unreconciled == {}


def test_every_corpus_file_round_trips(corpus: Path) -> None:
    """generate -> parse -> equals source, on files this module did not write.

    The property test round-trips claims the test itself constructed. This one
    starts from an independent writer's output, so it also catches a field the
    parser reads and the generator cannot represent.
    """
    failures: list[str] = []
    for path in sorted((corpus / "x12" / "837").glob("*.txt")):
        original = read_claims(path.read_bytes())[0]
        rebuilt = read_claims(build_837(original).encode())[0]
        if rebuilt.claims != original.claims:
            failures.append(path.name)

    assert not failures, f"{len(failures)} files did not round-trip: {failures[:5]}"


def test_taxonomy_is_the_billing_providers_not_the_rendering_providers(
    corpus: Path, expected: dict[str, object]
) -> None:
    """A limitation of the corpus, asserted so it cannot be mistaken later.

    `PRV*BI*PXC` sits in loop 2000A and carries the *billing* provider's
    taxonomy, written once for the file. `claim_headers.csv` records a
    per-claim `taxonomy_code`, which is the *rendering* provider's specialty --
    a different thing, carried in loop 2310B (`PRV*PE`), which the synthetic
    generator does not emit.

    So rendering-provider specialty is simply not recoverable from these files,
    and anything that needs it must read the CSV rather than the X12. This
    test exists because the two fields have the same name and the same shape,
    and the first reading of this comparison treated a correct parse as 19
    parser failures.
    """
    headers = expected["headers"]
    assert isinstance(headers, dict)

    varies_within_a_file = 0
    for path in sorted((corpus / "x12" / "837").glob("*.txt")):
        claims = read_claims(path.read_bytes())[0].claims
        taxonomies = {c.billing_provider.taxonomy_code for c in claims}
        # One file, one billing provider, therefore one taxonomy.
        assert len(taxonomies) == 1, f"{path.name} carries {taxonomies}"
        if len({headers[c.claim_id]["taxonomy_code"] for c in claims}) > 1:
            varies_within_a_file += 1

    # And the CSV genuinely disagrees with it, which is the point: if this ever
    # reaches zero the corpus has changed and the comparison above could be
    # tightened to include taxonomy.
    assert varies_within_a_file > 0
