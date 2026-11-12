"""The warehouse the leakage suite measures, built once (M2.4, M6.1).

Helpers for `conftest.py` and the suite's modules. The corpus is loaded and
dbt run once per suite: the feature-leakage tests and the ML pipeline tests
read the same build.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from rcm.ingestion.acknowledgement_loader import load_acknowledgement_file
from rcm.ingestion.claim_loader import load_claim_file
from rcm.ingestion.remittance_loader import load_remittance_file
from rcm.platform.db import session_scope

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def load_corpus(archive_root: Path) -> None:
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
