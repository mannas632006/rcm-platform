"""The synthetic corpus contains every case M1 has to handle (M0.10, CLAUDE.md section 8).

M1's gate is *"ingest the full synthetic corpus; every file balances or
quarantines"*, and the invariant tests in section 7.2 include multi-CAS lines,
resubmission chaining and reversal handling. None of those can be tested against
a corpus that does not contain them -- and a generator quietly stopping emitting
an edge case is invisible, because the parser tests go on passing while silently
covering less.

So the generator has a contract, asserted here: the corpus it produces must
contain each hard case, in enough volume to be more than a single fixture.

The smoke configuration is used deliberately. It is fast enough to run in the
unit suite, and if an edge case survives at that volume it survives at any
larger one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_CONFIG = REPO_ROOT / "tools" / "synthetic" / "config" / "smoke.yaml"


@pytest.fixture(scope="module")
def corpus() -> dict[str, object]:
    """Generate the smoke corpus once and hand back its manifest."""
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

    manifest = json.loads((OUT / "manifest.json").read_text())
    manifest["_out"] = OUT
    return manifest


def _x835(corpus: dict[str, object]) -> list[Path]:
    return sorted((corpus["_out"] / "x12" / "835").glob("*.txt"))  # type: ignore[operator]


def _x837(corpus: dict[str, object]) -> list[Path]:
    return sorted((corpus["_out"] / "x12" / "837").glob("*.txt"))  # type: ignore[operator]


def test_the_corpus_is_marked_as_synthetic(corpus: dict[str, object]) -> None:
    """Provenance travels with the data, so M6 can refuse to train on it.

    CLAUDE.md section 8: this corpus is for pipeline testing, not learning. A
    marker in the manifest is what lets that refusal be mechanical rather than
    a matter of whoever starts the training run remembering.
    """
    assert corpus["provenance"] == "synthetic:tools.synthetic"
    assert "Do not train a production model on this" in str(corpus["warning"])


def test_both_transaction_types_are_produced(corpus: dict[str, object]) -> None:
    assert _x837(corpus), "no 837P claim files"
    assert _x835(corpus), "no 835 remittance files"


@pytest.mark.invariant
def test_corpus_contains_multi_cas_lines(corpus: dict[str, object]) -> None:
    """PD-6 cannot be tested without a line carrying more than one CAS reason.

    This is the case that distinguishes service-line grain from adjustment
    grain. A parser that merges adjustments passes every test until it meets
    one of these.
    """
    multi = 0
    for path in _x835(corpus):
        cas_per_line = Counter()
        current = -1
        for segment in path.read_text().split("~"):
            fields = segment.strip().split("*")
            if fields[0] == "SVC":
                current += 1
            elif fields[0] == "CAS" and current >= 0:
                cas_per_line[current] += 1
        multi += sum(1 for count in cas_per_line.values() if count > 1)

    assert multi >= 10, f"only {multi} multi-CAS lines; PD-6 needs a real sample"


@pytest.mark.invariant
def test_corpus_contains_both_co_and_pr_group_codes(corpus: dict[str, object]) -> None:
    """CO-45 and PR-45 mean opposite things (PD-3, PD-19).

    CO is a contractual write-off the patient may never be billed for; PR is
    patient responsibility. A corpus containing only one of them would let a
    parser that drops the group code pass.
    """
    groups: Counter[str] = Counter()
    for path in _x835(corpus):
        for segment in path.read_text().split("~"):
            fields = segment.strip().split("*")
            if fields[0] == "CAS" and len(fields) > 1:
                groups[fields[1]] += 1

    assert groups["CO"] > 0, "no contractual adjustments"
    assert groups["PR"] > 0, "no patient-responsibility adjustments"


def test_corpus_contains_resubmissions(corpus: dict[str, object]) -> None:
    """PD-7: resubmissions chain to the original rather than becoming new claims."""
    counts = corpus["counts"]
    assert counts["resubmissions"] > 0  # type: ignore[index]
    assert counts["claims"] > counts["original_claims"]  # type: ignore[index]


def test_corpus_contains_recoupments(corpus: dict[str, object]) -> None:
    """A paid claim reversed months later must reopen, not create a negative claim."""
    assert corpus["counts"]["recouped_claims"] > 0  # type: ignore[index]


def test_corpus_contains_an_unbalanced_file_for_the_quarantine_path(
    corpus: dict[str, object],
) -> None:
    """M1.3 needs a file that must quarantine rather than partially load.

    Without one, the quarantine branch is never exercised and a parser that
    silently loads an unbalanced file looks correct.
    """
    unbalanced = [p for p in _x835(corpus) if "UNBALANCED" in p.name]
    assert len(unbalanced) == 1, "expected exactly one deliberate quarantine fixture"


def test_denial_rate_is_plausible(corpus: dict[str, object]) -> None:
    """A corpus with an implausible denial rate teaches the wrong shape.

    Published industry rates sit in the low to mid teens. This is not a
    statistical claim about reality -- it is a guard against a configuration
    change that makes every claim deny, which would make every downstream
    measurement meaningless while every test still passed.
    """
    rate = float(corpus["denial_rate_all_attempts"])  # type: ignore[arg-type]
    assert 0.05 <= rate <= 0.30, f"denial rate {rate:.2%} is outside the plausible band"


def test_the_independent_validator_passes(corpus: dict[str, object]) -> None:
    """The generator's output is checked by a parser that does not share its code.

    `validate.py` re-parses the emitted X12 from scratch rather than inspecting
    the structures that produced it. A round-trip through the generator's own
    objects would agree with itself regardless of whether the files are valid.
    """
    result = subprocess.run(
        [sys.executable, "-m", "tools.synthetic.validate"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
