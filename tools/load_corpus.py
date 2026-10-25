"""Load a generated synthetic corpus into CORE.

The generator writes X12 files to disk; the loaders read them into the
database. Until now nothing joined the two outside a pytest fixture, so
every path that needed a loaded database was a test that loaded it itself.
`make seed` has advertised "Generate and load the synthetic corpus" since M0
and only ever generated.

That gap surfaced in CI as a dbt build over an empty warehouse: the job
generated the corpus, ran `dbt build`, and every model selected zero rows.
`assert_the_warehouse_is_not_empty` caught it, which is the one piece of
good news in that run -- 44 assertions would otherwise have passed over
nothing.

Files are loaded in the order a claim meets them, because that order is not
cosmetic: a 277CA rejection must be recorded before the 835s that would
otherwise be the only account of what happened to the claim (PD-4), and
resubmission chaining needs the original present before the replacement
(BR-DATA-03, PD-7).

    python -m tools.load_corpus
    python -m tools.load_corpus --corpus data/synthetic --client <uuid>

Idempotent, like the loaders it calls: re-running recognises every file by
content hash and changes nothing (PD-14).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from rcm.ingestion.acknowledgement_loader import load_acknowledgement_file
from rcm.ingestion.claim_loader import load_claim_file
from rcm.ingestion.remittance_loader import load_remittance_file
from rcm.platform.db import session_scope

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "data" / "synthetic"

#: The client every seeded row belongs to (PD-20: scoping is per client, and
#: a corpus with no client would be unscopeable). Fixed rather than random so
#: that a reload lands in the same tenant and the idempotency check means
#: something.
DEFAULT_CLIENT = uuid.UUID("5eed0000-0000-4000-8000-00000000da7a")

#: Directory -> loader, in arrival order. A dict preserves insertion order,
#: and the order is the point.
STAGES: dict[str, Callable[..., Any]] = {
    "837": load_claim_file,
    "999": load_acknowledgement_file,
    "277CA": load_acknowledgement_file,
    "835": load_remittance_file,
}


def files(corpus: Path, folder: str) -> Iterator[Path]:
    return iter(sorted((corpus / "x12" / folder).glob("*.txt")))


def load(corpus: Path, *, client_id: uuid.UUID, archive_root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    quarantined = 0

    for folder, loader in STAGES.items():
        loaded = 0
        for path in files(corpus, folder):
            with session_scope() as session:
                result = loader(
                    session,
                    path.read_bytes(),
                    client_id=client_id,
                    original_filename=path.name,
                    archive_root=archive_root,
                )
            loaded += 1
            # A quarantine is a successful outcome, not a failure: the corpus
            # ships a deliberately unbalanced file so the control has
            # something to catch (M1.3). It is counted and reported rather
            # than raised on.
            if getattr(result, "quarantined", False):
                quarantined += 1
        counts[folder] = loaded

    counts["quarantined"] = quarantined
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--client", type=uuid.UUID, default=DEFAULT_CLIENT)
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="where RAW files are archived (PD-9); defaults to <corpus>/raw",
    )
    args = parser.parse_args(argv)

    corpus: Path = args.corpus
    if not (corpus / "x12").is_dir():
        sys.stderr.write(
            f"no corpus at {corpus}.\nRun `python -m tools.synthetic --config "
            "tools/synthetic/config/smoke.yaml` first.\n"
        )
        return 2

    archive_root: Path = args.archive or (corpus / "raw")
    archive_root.mkdir(parents=True, exist_ok=True)

    counts = load(corpus, client_id=args.client, archive_root=archive_root)

    total = sum(v for k, v in counts.items() if k != "quarantined")
    if total == 0:
        sys.stderr.write(f"the corpus at {corpus} contains no X12 files.\n")
        return 1

    for folder in STAGES:
        sys.stdout.write(f"  {folder:>6}  {counts[folder]}\n")
    sys.stdout.write(f"  {'files':>6}  {total}, quarantined {counts['quarantined']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
